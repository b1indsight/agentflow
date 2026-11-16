import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import app.controler.runtime as runtime_module
import app.worker.health as worker_health
from app.controler.api import create_app
from app.github import Batch
from app.persistence import readiness
from app.persistence.readiness import database_ready
from app.worker.health import HealthFile, healthy


async def test_database_readiness_is_read_only(postgresql_url):
    assert await database_ready(postgresql_url)


async def test_database_failure_is_not_liveness_failure(monkeypatch, server_runtime):
    async def fail(*args, **kwargs):
        raise OSError('private DSN secret')

    monkeypatch.setattr(readiness.psycopg.AsyncConnection, 'connect', fail)
    assert not await database_ready('unused')

    runtime = server_runtime()
    monkeypatch.setattr(type(runtime.coordinator.github), "fetch_installations",
                        AsyncMock(return_value=Batch()))
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


async def test_shutdown_during_probe_cannot_return_ready(monkeypatch, server_runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(_):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(runtime_module, 'database_ready', probe)
    runtime = server_runtime()
    monkeypatch.setattr(type(runtime.coordinator.github), "fetch_installations",
                        AsyncMock(return_value=Batch()))
    runtime.startup()
    task = asyncio.create_task(runtime.ready())
    await entered.wait()
    await runtime.shutdown()
    release.set()
    assert not await task
