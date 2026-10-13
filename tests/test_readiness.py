import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

import app.control.runtime as control_runtime
import app.worker.health as worker_health
from app.control.api import create_app
from app.persistence import readiness
from app.persistence.readiness import database_ready
from app.telemetry.metrics import Metrics
from app.worker.health import HealthFile, healthy


async def test_database_readiness_is_read_only(postgresql_url):
    assert await database_ready(postgresql_url)


async def test_database_failure_is_not_liveness_failure(monkeypatch):
    async def fail(*args, **kwargs):
        raise OSError('private DSN secret')

    monkeypatch.setattr(readiness.psycopg.AsyncConnection, 'connect', fail)
    assert not await database_ready('unused')

    runtime = Mock(spec=control_runtime.ControlRuntime,
                   settings=SimpleNamespace(host="test", port=0, metrics_token=None),
                   metrics=Metrics(), startup=Mock(), shutdown=AsyncMock(),
                   ready=AsyncMock(return_value=False))
    with TestClient(create_app(runtime)) as client:
        assert client.get('/healthz').status_code == 200
        response = client.get('/readyz')
        assert response.status_code == 503
        assert 'secret' not in response.text


async def test_probe_connection_deadline_does_not_block_event_loop(monkeypatch):
    async def blocked(*args, **kwargs): await asyncio.Event().wait()
    monkeypatch.setattr(readiness.psycopg.AsyncConnection, 'connect', blocked)
    task = asyncio.create_task(database_ready('unused'))
    await asyncio.sleep(0)
    assert not task.done()
    assert not await asyncio.wait_for(task, timeout=3)


def test_worker_probe_is_connection_freshness_not_job_idleness(tmp_path, monkeypatch):
    path = tmp_path / 'health.json'
    clock = [100.0]
    monkeypatch.setattr(worker_health.time, 'time', lambda: clock[0])
    marker = HealthFile(path)
    marker.write(False)
    assert healthy(path, live=True)
    assert not healthy(path)
    marker.write(True)
    assert healthy(path)  # Busy executors retain connected state.
    clock[0] += 21
    assert not healthy(path)
    marker.write(False)
    assert not healthy(path)


async def test_shutdown_during_probe_cannot_return_ready(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(_):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(control_runtime, 'database_ready', probe)
    runtime = control_runtime.ControlRuntime.__new__(control_runtime.ControlRuntime)
    runtime.settings = SimpleNamespace(database_url='unused')
    runtime._ready, runtime._readiness_lock = True, asyncio.Lock()
    task = asyncio.create_task(runtime.ready())
    await entered.wait()
    runtime._ready = False
    release.set()
    assert not await task
