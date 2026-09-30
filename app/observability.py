"""Sanitized process logs and authenticated, cached Prometheus metrics."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import sys
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, TextIO

import psycopg
from loguru import logger
from prometheus_client import CollectorRegistry, Counter, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric

from app.metrics_store import Snapshot, read_snapshot

if TYPE_CHECKING:
    from loguru import Message, Record

EVENTS = {'job_failed', 'job_started', 'job_result', 'webhook_received', 'webhook_applied',
          'worker_connected', 'worker_disconnected', 'snapshot_failed', 'application_log'}
REASONS = {'ok', 'execution_failed', 'database_unavailable', 'connection_lost', 'unspecified'}


def configure_logging(role: str, output_format: str, output: TextIO | None = None) -> None:
    """Replace sinks: raw messages, exception values and arbitrary extras never reach output."""
    target = output or sys.stdout

    def emit(message: Message) -> None:
        record: Record = message.record
        extra = record['extra']
        event = extra.get('event')
        reason = extra.get('reason_code')
        row: dict[str, str | int] = {
            'timestamp': record['time'].isoformat(), 'level': record['level'].name,
            'role': role, 'event': event if isinstance(event, str) and event in EVENTS else 'application_log',
            'reason_code': reason if isinstance(reason, str) and reason in REASONS else 'unspecified',
            'source': f"{record['name']}:{record['function']}:{record['line']}",
        }
        for key in ('flow_id', 'job_id', 'protocol_version'):
            if type(extra.get(key)) is int:
                row[key] = extra[key]
        for key in ('delivery_id', 'worker_id', 'epoch'):
            value = extra.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
                row[key] = value
        exception = record['exception']
        if exception and exception.type:
            row['exception_type'] = exception.type.__name__
        if output_format == 'json':
            rendered = json.dumps(row, ensure_ascii=True)
        else:
            rendered = ' '.join(f'{key}={value}' for key, value in row.items())
        target.write(rendered + '\n')
        target.flush()

    logger.remove()
    logger.add(emit, format='{message}', backtrace=False, diagnose=False, catch=False)
    # Standard logging also carries third-party HTTP/DSN exception messages.
    class SanitizedHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            def origin(target_record: Record) -> None:
                # Preserve code identity, never arbitrary logger names containing URL/credentials.
                target_record['name'] = record.name if re.fullmatch(r'[A-Za-z0-9_.]{1,200}', record.name) else 'standard_logging'
                target_record['function'] = record.funcName if re.fullmatch(r'[A-Za-z0-9_.<>]{1,200}', record.funcName or '') else 'unknown'
                target_record['line'] = record.lineno
            level: str | int = record.levelno
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                pass  # Custom standard levels retain their numeric severity.
            logger.patch(origin).opt(exception=record.exc_info).log(level, '')

    logging.basicConfig(handlers=[SanitizedHandler()], level=logging.INFO, force=True)
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        standard = logging.getLogger(name)
        standard.handlers.clear()
        standard.propagate = True


KINDS = ('initial', 'triage', 'revision', 'answer', 'archive', 'other')
FAMILIES: dict[str, tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]] = {
    'inbox': (('state',), (('pending', 'processing', 'done', 'blocked', 'other'),)),
    'jobs': (('kind', 'state'), (KINDS, ('queued', 'running', 'done', 'failed', 'other'))),
    'workers': (('state',), (('online', 'busy', 'recovering', 'offline', 'failed', 'other'),)),
    'bindings_active': ((), ()), 'recovery_pending': ((), ()), 'reconcile_due': ((), ()),
    'archive_pending_age_seconds': ((), ()),
    'effects': (('state',), (('pending', 'running', 'retry', 'uncertain', 'succeeded', 'failed', 'skipped', 'other'),)),
    'projects': (('state',), (('pending', 'blocked', 'other'),)),
    'oldest_age_seconds': (('queue',), (('inbox', 'jobs', 'recovery', 'archive', 'reconcile', 'projects', 'other'),)),
    'execution_receipts': (('kind',), (KINDS,)),
    'execution_elapsed_seconds': (('kind',), (KINDS,)),
}


class Metrics:
    """Single-process counters plus atomically replaced persistent-fact gauges."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.snapshot: Snapshot | None = None
        self.updated: float | None = None
        self.started = time.monotonic()
        self.errors = Counter('flowtool_metrics_snapshot_errors', 'Failed background snapshots', registry=self.registry)
        self.webhooks = Counter('flowtool_webhook_requests', 'Process HTTP requests, including redelivery',
                                ['status'], registry=self.registry)
        self.replays = Counter("flowtool_result_replay_observations", "Authenticated recovery of an already applied result", registry=self.registry)
        self.expired = Counter("flowtool_lease_expiry_transitions", "This process changed an expired assignment", ["outcome"], registry=self.registry)
        self.registry.register(self)

    def request(self, status: int) -> None:
        label = str(status) if status in (202, 400, 401, 409, 413, 503) else 'other'
        self.webhooks.labels(label).inc()

    def accept(self, snapshot: Snapshot) -> None:
        self.snapshot, self.updated = snapshot, time.monotonic()

    def failed(self) -> None:
        self.errors.inc()
        logger.bind(event='snapshot_failed', reason_code='database_unavailable').warning('')

    def collect(self) -> Iterator[Metric]:
        snapshot = self.snapshot
        yield GaugeMetricFamily('flowtool_metrics_snapshot_ready', 'Has at least one successful snapshot', value=float(snapshot is not None))
        yield GaugeMetricFamily('flowtool_metrics_snapshot_age_seconds', 'Seconds since last success or process start',
                                value=max(0, time.monotonic() - (self.updated if self.updated is not None else self.started)))
        if snapshot is None:
            return
        for family, (names, allowed) in FAMILIES.items():
            values = dict.fromkeys(itertools.product(*allowed), 0.0)
            for sample in snapshot.samples:
                if sample.family != family or len(sample.labels) != len(names):
                    continue
                labels = tuple(label if label in choices else 'other' for label, choices in zip(sample.labels, allowed, strict=True))
                values[labels] += sample.value
            metric = GaugeMetricFamily(f'flowtool_{family}', f'Persistent snapshot: {family}', labels=names)
            for labels, value in values.items():
                metric.add_metric(labels, value)
            yield metric

    def render(self) -> bytes:
        return generate_latest(self.registry)

    async def run(self, database_url: str, interval: float) -> None:
        """One in-flight query; finish its bounded connection before shutdown."""
        while True:
            task = asyncio.create_task(asyncio.to_thread(read_snapshot, database_url))
            try:
                self.accept(await asyncio.shield(task))
            except asyncio.CancelledError:
                await asyncio.gather(task, return_exceptions=True)
                raise
            except (OSError, psycopg.Error):
                self.failed()
            await asyncio.sleep(interval)
