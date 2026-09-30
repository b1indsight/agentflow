"""Sanitized process logging shared by control and workers without database imports."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import TYPE_CHECKING, TextIO

from loguru import logger

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
