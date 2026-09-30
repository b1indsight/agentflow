import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.control.api import create_app
from app.persistence.readiness import database_ready
from app.telemetry.metrics import Metrics
from app.worker.health import HealthFile, healthy


async def test_database_readiness_is_read_only(postgresql_url):
    assert await database_ready(postgresql_url)


async def test_database_failure_is_not_liveness_failure(monkeypatch):
    import app.persistence.readiness as module

    async def fail(*args, **kwargs):
        raise OSError('private DSN secret')

    monkeypatch.setattr(module.psycopg.AsyncConnection, 'connect', fail)
    assert not await database_ready('unused')

    class Context:
        settings = SimpleNamespace(host="test", port=0, metrics_token=None)
        metrics = Metrics()
        def startup(self): pass
        async def shutdown(self): pass
        async def ready(self): return False

    with TestClient(create_app(Context)) as client:
        assert client.get('/healthz').status_code == 200
        response = client.get('/readyz')
        assert response.status_code == 503
        assert 'secret' not in response.text


async def test_probe_connection_deadline_does_not_block_event_loop(monkeypatch):
    import app.persistence.readiness as module
    async def blocked(*args, **kwargs): await asyncio.Event().wait()
    monkeypatch.setattr(module.psycopg.AsyncConnection, 'connect', blocked)
    task = asyncio.create_task(database_ready('unused'))
    await asyncio.sleep(0)
    assert not task.done()
    assert not await asyncio.wait_for(task, timeout=3)


def test_worker_probe_is_connection_freshness_not_job_idleness(tmp_path, monkeypatch):
    import app.worker.health as module
    path = tmp_path / 'health.json'
    clock = [100.0]
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
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
    path.write_text('{truncated')
    assert not healthy(path, live=True)


async def test_shutdown_during_probe_cannot_return_ready(monkeypatch):
    import app.control.runtime as module
    entered, release = asyncio.Event(), asyncio.Event()
    async def probe(_):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(module, 'database_ready', probe)
    runtime = module.ControlRuntime.__new__(module.ControlRuntime)
    runtime.settings = SimpleNamespace(database_url='unused')
    runtime._ready, runtime._readiness_lock = True, asyncio.Lock()
    task = asyncio.create_task(runtime.ready())
    await entered.wait()
    runtime._ready = False
    release.set()
    assert not await task
