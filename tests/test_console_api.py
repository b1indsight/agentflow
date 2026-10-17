"""Owner authentication, whitelist reads and installed static routing contracts."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from app.controler.api import create_app
from app.controler.console.sync import ConsoleSynchronizer
from app.github.console import Observation
from app.persistence.activity import ActivityStore
from app.telemetry.metrics import Metrics
from tests.access import AccessIssuer


@pytest.fixture
def console_runtime(store):
    issuer = AccessIssuer()
    return SimpleNamespace(
        database=store._db,
        settings=issuer.settings,
        access=issuer.auth,
        issuer=issuer,
        metrics=Metrics(),
        startup=lambda: None,
        shutdown=issuer.auth.close,
        console=ConsoleSynchronizer(SimpleNamespace(), SimpleNamespace(), database=store._db),
        conversations=SimpleNamespace(read=AsyncMock()),
    )


def test_console_and_every_public_read_require_owner_before_io(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    with TestClient(create_app(console_runtime)) as client:
        paths = [
            "/console",
            "/console/",
            "/console/assets/missing.js",
            "/api/repos",
            "/api/flows",
            f"/api/flows/{flow.id}",
            f"/api/flows/{flow.id}/jobs",
            f"/api/flows/{flow.id}/activity",
            f"/api/flows/{flow.id}/activity/content?key=x&version=1",
            f"/api/flows/{flow.id}/activity/versions?key=x",
        ]
        for path in paths:
            assert client.get(path, headers={"If-None-Match": "anything"}).status_code == 401
        headers = console_runtime.issuer.headers()
        response = client.get("/api/flows?q=repo:owner/repo", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["counts"]["queued"] == 1
        assert "installation_id" not in response.text and "payload" not in response.text
        assert client.get("/api/flows/99999", headers=headers).status_code == 404
        assert client.get("/api/flows?limit=101", headers=headers).status_code == 422
        assert client.get("/api/flows?cursor=garbage", headers=headers).status_code == 409
        assert client.get("/api/flows?q=secret:bad", headers=headers).status_code == 422
        body = client.get(f"/api/flows/{flow.id}/activity", headers=headers).json()
        item = body["items"][0]
        assert (
            client.get(
                f"/api/flows/{flow.id}/activity/content",
                headers=headers,
                params={"key": item["key"], "version": item["version"]},
            ).status_code
            == 200
        )
        console_runtime.settings.access_owner_email = None
        assert client.get("/console/", headers=headers).status_code == 503


def test_edited_hint_and_diff_compare_full_immutable_content_not_preview(console_runtime, store):
    flow = store.flows.create_flow("owner/repo", 7, 42)
    archive = ActivityStore(store._db)
    original = Observation(
        key="comment:7",
        source="issue.comments",
        kind="comment",
        author="owner",
        occurred_at=flow.created_at,
        updated_at=flow.created_at,
        body="共有正文" * 5000 + "\n最初的结论😀",
    )
    archive.append(flow.id, original)
    with TestClient(create_app(console_runtime)) as client:
        headers = console_runtime.issuer.headers()
        path = f"/api/flows/{flow.id}/activity"

        def comment():
            return next(
                i
                for i in client.get(path, headers=headers).json()["items"]
                if i["key"] == original.key
            )

        assert comment()["previous_version"] is None
        # An extra observation with unchanged text must not advertise an edit.
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "metadata": {"state": "observed"},
                    "updated_at": flow.created_at + timedelta(seconds=1),
                }
            ),
        )
        assert comment()["previous_version"] is None
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "body": original.body.replace("最初", "修改后"),
                    "updated_at": flow.created_at + timedelta(seconds=2),
                }
            ),
        )
        edited = comment()
        assert edited["previous_version"] is not None
        params = {"key": original.key, "version": edited["version"], "compare": True}
        assert client.get(path + "/content", params=params).status_code == 401
        diff = client.get(path + "/content", headers=headers, params=params).json()
        assert "-最初的结论😀" in diff["text"] and "+修改后的结论😀" in diff["text"]
        assert diff["previous_version"] == edited["previous_version"]
        # A long context line crosses the old 16K response boundary: parsers need the entire patch.
        assert len(diff["text"]) > 16384
        assert diff["next_offset"] is None and diff["total"] == len(diff["text"])
        assert (
            client.get(path + "/content", headers=headers, params={**params, "limit": 20}).json()
            == diff
        )
        assert (
            client.get(
                path + "/content", headers=headers, params={**params, "offset": 1}
            ).status_code
            == 422
        )
        archive.append(
            flow.id,
            original.model_copy(
                update={
                    "body": "x" * 65537,
                    "updated_at": flow.created_at + timedelta(seconds=3),
                }
            ),
        )
        # Later ingestion cannot change the pair selected for the old reading version.
        assert client.get(path + "/content", headers=headers, params=params).json() == diff
        params["version"] = comment()["version"]
        response = client.get(path + "/content", headers=headers, params=params)
        assert response.status_code == 422 and response.json()["detail"] == "diff_too_large"
