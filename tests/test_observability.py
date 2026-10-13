import asyncio
import io
import json
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr

import app.controler.config as server_config
import app.controler.runtime as server_runtime
import app.telemetry.metrics as metrics_module
from app import cli
from app.contracts.protocol import PROTOCOL_VERSION, InitialTaskResult, Succeeded
from app.controler.api import create_app
from app.controler.config import ServerSettings
from app.domain.models import DeliveryState, FlowState, JobKind, JobState, utcnow
from app.persistence import metrics_store
from app.persistence.metrics_store import read_snapshot
from app.persistence.webhook_store import WebhookStore
from app.telemetry.logging import configure_logging
from app.telemetry.metrics import Metrics


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
            self.settings = SimpleNamespace(metrics_token=SecretStr('monitor-only'), host="test", port=0)
            self.metrics = Metrics()
        def startup(self): pass
        async def shutdown(self): pass

    context = Context()
    with TestClient(create_app(context)) as client:
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
    started, release = threading.Event(), threading.Event()
    calls = []

    def blocked_read(_):
        started.set()
        release.wait(2)
        calls.append(1)
        raise OSError('private database DSN')

    monkeypatch.setattr(metrics_module, 'read_snapshot', blocked_read)
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
    flow = store.create_flow('private/repository', 1, 1)
    job = store.enqueue_job(flow.id, JobKind.INITIAL)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    assert store.assign_job('w', 'token', 'epoch')
    result = Succeeded(job_id=job.id, assignment_token='token', connection_epoch='epoch',
                       result=InitialTaskResult(session_id='session', last_message='', exit_code=1))
    assert store.record_assignment_result(job.id, 'w', 'token', 'epoch',
                                          result.model_dump(mode='json'), 'digest') == 'pending'
    with store.connection() as connection:
        connection.execute("UPDATE jobs SET started_at=result_received_at-interval '3 seconds' WHERE id=%s", (job.id,))
    metrics = Metrics()
    for _ in range(2):
        metrics.accept(read_snapshot(postgresql_url))
        rendered = metrics.render().decode()
        assert 'flowtool_execution_receipts{kind="initial"} 1.0' in rendered
        assert 'flowtool_execution_elapsed_seconds{kind="initial"} 3.0' in rendered
    assert 'private/repository' not in rendered


def test_snapshot_query_timeout_preserves_previous_facts(store, postgresql_url, monkeypatch):
    metrics = Metrics()
    metrics.accept(read_snapshot(postgresql_url))
    previous = metrics.snapshot
    monkeypatch.setattr(metrics_store, 'SNAPSHOT_SQL', 'SELECT pg_sleep(10)')
    with pytest.raises(psycopg.errors.QueryCanceled): read_snapshot(postgresql_url)
    metrics.failed()
    assert metrics.snapshot is previous
    assert b'flowtool_metrics_snapshot_errors_total 1.0' in metrics.render()


def test_standard_logging_preserves_level_and_origin_without_payload():
    output = io.StringIO()
    configure_logging('serve', 'json', output)
    try:
        source = logging.getLogger('fixture.original')
        for level in (logging.WARNING, logging.ERROR):
            record = source.makeRecord(source.name, level, '/private/secret/path', 123,
                                       'private token %s', ('credential',), None, 'original_fn')
            source.handle(record)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert [row['level'] for row in rows] == ['WARNING', 'ERROR']
        assert {row['source'] for row in rows} == {'fixture.original:original_fn:123'}
        assert 'credential' not in output.getvalue() and '/private' not in output.getvalue()
    finally:
        logger.remove()


def test_serve_preserves_sanitized_uvicorn_logging(monkeypatch, capsys):
    output = io.StringIO()
    settings = ServerSettings.model_construct(database_url="postgresql://localhost/test",
        worker_tokens={"worker-1": SecretStr("worker-secret")}, log_format="json")
    monkeypatch.setattr(server_config, "ServerSettings", lambda: settings)
    monkeypatch.setattr(server_runtime, "Store", Mock())
    monkeypatch.setattr(server_runtime, "GitHubApp", Mock())
    monkeypatch.setattr(server_runtime, "configure_logging",
                        lambda role, output_format: configure_logging(role, output_format, output))

    def initialize_server(application, **kwargs):
        # CLI constructs the runtime first; Uvicorn configures its handlers afterwards.
        cli.uvicorn.Config(application, **kwargs)
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1", "GET", "/probe?token=secret-query", "1.1", 200)
        try:
            raise RuntimeError("private secret-error")
        except RuntimeError:
            logging.getLogger("uvicorn.error").exception("raw exception payload")

    monkeypatch.setattr(cli.uvicorn, "run", initialize_server)
    try:
        cli.serve()
        captured = capsys.readouterr()
        rendered = output.getvalue() + captured.out + captured.err
        assert not any(secret in rendered for secret in ("secret-query", "secret-error", "raw exception payload"))
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        assert {row["level"] for row in rows} == {"INFO", "ERROR"}
        assert rows[-1]["exception_type"] == "RuntimeError"
    finally:
        logger.remove()


def test_reconcile_metrics_match_scheduler_eligibility(store, postgresql_url):
    for number, state, delivery_state, pr in [
        (1, FlowState.ACTIVE, DeliveryState.OPEN, 11),
        (2, FlowState.CANCELLED, DeliveryState.OPEN, 12),
        (4, FlowState.ACTIVE, DeliveryState.MERGED, 14),
        (5, FlowState.ACTIVE, DeliveryState.NONE, None),
    ]:
        flow = store.create_flow('fixture/repo', number, 1)
        flow.state = state
        store.update_flow(flow)
        delivery = store.get_or_create_delivery(flow.id, f'flow/{number}')
        delivery.state, delivery.pr_number = delivery_state, pr
        store.update_delivery(delivery)
        store.reconcile.hint(flow.id)
        with store.connection() as conn:
            conn.execute("UPDATE flow_reconcile SET next_at=now()-(%s * interval '1 second') WHERE flow_id=%s",
                         (60 if number <= 2 else 3600, flow.id))
    due = store.reconcile.due(100)
    assert len(due) == 2  # Cancelled PRs still need reconciliation; merged/no-PR do not.
    metrics = read_snapshot(postgresql_url)
    assert next(s.value for s in metrics.samples if s.family == 'reconcile_due') == len(due)
    age = next(s.value for s in metrics.samples if s.family == 'oldest_age_seconds' and s.labels == ['reconcile'])
    assert 60 <= age < 120


def test_archive_alert_age_uses_latest_job_of_active_bound_flow(store, postgresql_url):
    flow = store.create_flow('fixture/recovery', 1, 1)
    store.worker_connected('w', PROTOCOL_VERSION, 'epoch')
    initial = store.enqueue_job(flow.id, JobKind.INITIAL)
    assert store.assign_job('w', 'token', 'epoch')
    assert store.finish_assignment(initial.id, 'w', 'token', JobState.DONE, 'epoch')
    delivery = store.get_or_create_delivery(flow.id, 'flow/1')
    delivery.state, delivery.pr_number = DeliveryState.MERGED, 17
    delivery.head_sha, delivery.merge_commit_sha, delivery.merged_at = 'head', 'merge', utcnow()
    store.update_delivery(delivery)
    job = store.enqueue_job(flow.id, JobKind.ARCHIVE)
    with store.connection() as conn:
        conn.execute("UPDATE jobs SET created_at=now()-interval '20 minutes' WHERE id=%s", (job.id,))
    def age():
        return next(s.value for s in read_snapshot(postgresql_url).samples
                    if s.family == 'archive_pending_age_seconds')
    assert age() >= 1200
    assert store.assign_job('w', 'archive-token', 'epoch')
    assert store.finish_assignment(job.id, 'w', 'archive-token', JobState.FAILED, 'epoch')
    # A newer queued retry replaces the age of the failed attempt.
    retry = store.enqueue_job(flow.id, JobKind.ARCHIVE)
    assert age() < 10
    # Successful archive application also terminates the Flow and releases its binding.
    assert store.assign_job('w', 'retry-token', 'epoch')
    assert store.finish_assignment(retry.id, 'w', 'retry-token', JobState.DONE, 'epoch')
    flow.state = FlowState.SUCCEEDED
    store.update_flow(flow)
    assert store.release_flow_execution(flow.id)
    assert age() == 0
