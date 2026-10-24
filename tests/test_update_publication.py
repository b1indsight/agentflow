"""Bind native releases to verified fixed-repository publication attempts."""

import copy

import pytest

from app import releases
from app.releases import ReleaseEvent


@pytest.mark.parametrize("fault", ["", "repository", "attempt", "event", "step", "head"])
def test_native_publication_requires_successful_main_push_and_exact_attempt(monkeypatch, fault):
    release = ReleaseEvent(run_id=1, attempt=1, run_number=1, sha="2" * 40)
    run = {
        "id": release.run_id,
        "run_attempt": release.attempt,
        "run_number": release.run_number,
        "head_sha": release.sha,
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
    head = {"object": {"sha": release.sha}}
    if fault == "repository":
        run["repository"] = {"full_name": "other/repository"}
    elif fault == "attempt":
        run["run_attempt"] = 2
    elif fault == "event":
        run["event"] = "pull_request"
    elif fault == "step":
        steps[-1]["conclusion"] = "skipped"
    elif fault == "head":
        head["object"]["sha"] = "3" * 40

    def get(model, path):
        return model.model_validate(
            copy.deepcopy(jobs if "/jobs?" in path else head if path.startswith("git/") else run)
        )

    monkeypatch.setattr(releases, "_get", get)
    if fault:
        with pytest.raises(releases.ReleaseError):
            releases.verify_release(release)
    else:
        assert releases.verify_release(release) == release
