import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr

from app.control.api import create_app
from app.metrics_store import read_snapshot
from app.observability import Metrics, configure_logging
from app.webhook_store import WebhookStore


async def test_logs_isolate_concurrent_context_and_never_render_exception_payload():
    output = io.StringIO()
    configure_logging('serve', 'json', output)

    async def task(flow_id):
        with logger.contextualize(flow_id=flow_id, assignment_token='secret-token'):
            await asyncio.sleep(0)
            try:
                raise ValueError('postgres://private:password@db/payload')
            except ValueError:
                logger.bind(event='job_failed', reason_code='execution_failed').exception('raw prompt secret')

    try:
        await asyncio.gather(task(1), task(2))
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert {row['flow_id'] for row in rows} == {1, 2}
        assert all(row['exception_type'] == 'ValueError' for row in rows)
        assert all(row['event'] == 'job_failed' for row in rows)
        for secret in ('secret-token', 'password', 'raw prompt', 'postgres://', 'payload'):
            assert secret not in output.getvalue()
    finally:
        logger.remove()


def test_metrics_default_off_and_bearer_protected():
    class Context:
        def __init__(self):
            self.settings = SimpleNamespace(metrics_token=SecretStr('monitor-only'))
            self.metrics = Metrics()
        def startup(self): pass
        async def shutdown(self): pass

    context = Context()
    with TestClient(create_app(lambda: context)) as client:
        assert client.get('/metrics').status_code == 401
        assert client.get('/metrics', headers={'Authorization': 'Bearer worker-token'}).status_code == 401
        response = client.get('/metrics', headers={'Authorization': 'Bearer monitor-only'})
        assert response.status_code == 200
        assert 'flowtool_metrics_snapshot_ready 0' in response.text
        assert 'flowtool_inbox' not in response.text
        context.settings.metrics_token = None
        assert client.get('/metrics', headers={'Authorization': 'Bearer monitor-only'}).status_code == 404


def test_persistent_backlog_and_empty_age(store, postgresql_url):
    WebhookStore(store).receive('sensitive-delivery-id', 'ping', {}, 'a' * 64)
    snapshot = read_snapshot(postgresql_url)
    metrics = Metrics()
    metrics.accept(snapshot)
    text = metrics.render().decode()
    assert 'flowtool_inbox{state="pending"} 1.0' in text
    assert 'flowtool_jobs{kind="initial",state="queued"} 0.0' in text
    assert 'flowtool_oldest_age_seconds{queue="jobs"} 0.0' in text
    assert 'sensitive-delivery-id' not in text and 'aaaaaa' not in text
    # DB failure must retain backlog and expose staleness, not replace it with zero.
    metrics.failed()
    text = metrics.render().decode()
    assert 'flowtool_inbox{state="pending"} 1.0' in text
    assert 'flowtool_metrics_snapshot_errors_total 1.0' in text


async def test_snapshot_runs_off_event_loop_and_scrape_never_reads_database(monkeypatch):
    import threading

    import app.observability as module
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocked_read(_):
        started.set()
        release.wait(2)
        calls.append(1)
        raise OSError('private database DSN')

    monkeypatch.setattr(module, 'read_snapshot', blocked_read)
    metrics = Metrics()
    task = asyncio.create_task(metrics.run('unused', 15))
    try:
        while not started.is_set(): await asyncio.sleep(0.001)
        for _ in range(3): assert b'flowtool_metrics_snapshot_ready 0' in metrics.render()
        assert not calls
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task


def test_repeated_snapshots_do_not_count_execution_receipt_twice(store, postgresql_url):
    from app.models import JobKind
    flow = store.create_flow('private/repository', 1, 1)
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET started_at=now()-interval '3 seconds', result_received_at=now() WHERE id=%s", (job.id,))
    metrics = Metrics()
    for _ in range(2):
        metrics.accept(read_snapshot(postgresql_url))
        rendered = metrics.render().decode()
        assert 'flowtool_execution_receipts{kind="initial"} 1.0' in rendered
        assert 'flowtool_execution_elapsed_seconds{kind="initial"} 3.0' in rendered
    assert 'private/repository' not in rendered


def test_snapshot_query_timeout_preserves_previous_facts(store, postgresql_url, monkeypatch):
    import psycopg

    import app.metrics_store as module
    metrics = Metrics()
    metrics.accept(read_snapshot(postgresql_url))
    previous = metrics.snapshot
    monkeypatch.setattr(module, 'SNAPSHOT_SQL', 'SELECT pg_sleep(10)')
    with pytest.raises(psycopg.errors.QueryCanceled): read_snapshot(postgresql_url)
    metrics.failed()
    assert metrics.snapshot is previous
    assert b'flowtool_metrics_snapshot_errors_total 1.0' in metrics.render()


def test_standard_logging_and_nested_extras_are_sanitized():
    import logging
    output = io.StringIO()
    configure_logging('worker', 'text', output)
    try:
        logger.bind(event={'authorization': 'Bearer secret'}, payload={'key': 'secret'},
                    reason_code=['secret']).error('https://private/?token=secret')
        logging.getLogger('uvicorn.access').error('secret DSN password')
        assert 'secret' not in output.getvalue()
        assert len(output.getvalue().splitlines()) == 2
    finally:
        logger.remove()
