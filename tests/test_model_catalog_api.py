from contextlib import AsyncExitStack

import httpx

from app.controler.api import create_app
from app.worker.config import WorkerSettings
from app.worker.runtime import WorkerRuntime
from tests.access import AccessIssuer
from tests.test_remote_e2e import remote_connection
from tests.test_workspace_dispatch import wait_until


async def test_catalog_unions_live_workers_and_survives_partial_failure(server_runtime, tmp_path, monkeypatch):
    # A rolling upgrade exposes a new model or effort on only one of two Codex workers.
    from app.contracts.catalog import CatalogModel
    from app.worker.model_catalog import CatalogUnavailable

    def model(name, *efforts):
        return CatalogModel(id=name, displayName=name, efforts=list(efforts))

    codex = iter([[model("a"), model("b", "low")], [model("b", "high"), model("c")],
                  [model("d")], None])

    async def discover(agent):
        result = next(codex) if agent == "codex" else []
        if result is None:
            raise CatalogUnavailable()
        return result

    monkeypatch.setattr("app.worker.transport.business.discover_models", discover)
    runtime = server_runtime()
    issuer = AccessIssuer()
    await runtime.access.close()
    runtime.access = issuer.auth
    app = create_app(runtime)
    app.state.runtime = runtime
    path = "/api/settings/model-catalog"
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ))
        assert (await http.get(path)).status_code == 401
        http.headers.update(issuer.headers())
        assert (await http.get(path)).json()["codex"] == {"status": "unavailable", "models": []}
        for i, agent in enumerate(("codex", "amp", "codex")):
            worker = WorkerRuntime(WorkerSettings(worker_id=f"w{i}", worker_token="worker-secret",
                                   coding_agent=agent, serve_ws_url="wss://unused", worker_root=tmp_path / str(i)))
            await stack.enter_async_context(remote_connection(runtime, worker))
            await wait_until(lambda worker=worker: worker._connected)
        before = (await http.get("/api/settings")).json()
        first = (await http.get(path)).json()
        assert first["amp"] == {"status": "ok", "models": []}
        assert first["codex"]["status"] == "ok"
        assert {m["id"]: sorted(m["efforts"]) for m in first["codex"]["models"]} == {
            "a": [], "b": ["high", "low"], "c": [],
        }
        assert (await http.get(path)).json()["codex"] == {
            "status": "partial", "models": [model("d").model_dump()],
        }
        assert (await http.get("/api/settings")).json() == before
