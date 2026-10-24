"""Bind native releases to verified fixed-repository publication attempts."""

import httpx
import pytest

from app import releases


@pytest.mark.parametrize("fault", ["", "repository", "running", "event", "step", "head"])
def test_native_publication_requires_successful_main_push_and_exact_attempt(monkeypatch, fault):
    sha = "2" * 40
    run = {
        "id": 1,
        "run_attempt": 2,
        "head_sha": sha,
        "path": ".github/workflows/images.yml",
        "event": "push",
        "head_branch": "main",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": "b1indsight/agentflow"},
    }
    steps = [
        {"name": name, "conclusion": "success"}
        for name in (
            "Build serve candidate with attestations",
            "Build worker candidate with attestations",
            "Pull and verify exact candidate digests",
            "Tag verified manifests without rebuilding",
        )
    ]
    jobs = {
        "total_count": 1,
        "jobs": [{"name": "publish", "conclusion": "success", "steps": steps}],
    }
    head = {"object": {"sha": sha}}
    if fault == "repository":
        run["repository"] = {"full_name": "other/repository"}
    elif fault == "running":
        run["status"] = "in_progress"
    elif fault == "event":
        run["event"] = "pull_request"
    elif fault == "step":
        steps[-1]["conclusion"] = "skipped"
    elif fault == "head":
        head["object"]["sha"] = "3" * 40

    responses = {
        "actions/workflows/images.yml/runs?branch=main&event=push&status=success&per_page=1": {
            "workflow_runs": [run]
        },
        "actions/runs/1/attempts/2/jobs?per_page=100": jobs,
        "git/ref/heads/main": head,
    }
    requested = []

    def respond(request):
        path = str(request.url).removeprefix("https://api.github.com/repos/b1indsight/agentflow/")
        assert path in responses, f"unexpected release request: {path}"
        requested.append(path)
        return httpx.Response(200, json=responses[path])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(releases.httpx, "stream", client.stream)
        if fault:
            with pytest.raises(releases.ReleaseError):
                releases.latest_release()
        else:
            assert releases.latest_release() == sha
            # The list already contains the run; pin its attempt without fetching it again.
            assert requested == list(responses)
