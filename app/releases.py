"""Read-only verification of the fixed public Images/main publication contract."""

import httpx
from pydantic import BaseModel, Field, ValidationError

REPOSITORY = "b1indsight/agentflow"


class ReleaseError(RuntimeError):
    """A public release cannot be verified; diagnostics never contain credentials."""


class _Repository(BaseModel):
    full_name: str


class _Run(BaseModel):
    id: int = Field(gt=0, le=10**18)
    run_attempt: int = Field(gt=0, le=10**9)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str
    event: str
    head_branch: str
    status: str
    conclusion: str | None
    repository: _Repository


class _Runs(BaseModel):
    workflow_runs: list[_Run]


class _Step(BaseModel):
    name: str
    conclusion: str | None


class _Job(_Step):
    steps: list[_Step]


class _Jobs(BaseModel):
    total_count: int
    jobs: list[_Job]


class _Object(BaseModel):
    sha: str


class _Ref(BaseModel):
    object: _Object


def _get[T: BaseModel](model: type[T], path: str) -> T:
    try:
        with httpx.stream(
            "GET",
            f"https://api.github.com/repos/{REPOSITORY}/{path}",
            timeout=20,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentflow-updates",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ReleaseError("public release metadata exceeds the size limit")
        return model.model_validate_json(body)
    except (httpx.HTTPError, ValidationError):
        raise ReleaseError("public release metadata is unavailable or invalid") from None


def latest_release() -> str:
    """Return the latest listed SHA after verifying its fixed attempt, steps and main head."""
    runs = _get(
        _Runs, "actions/workflows/images.yml/runs?branch=main&event=push&status=success&per_page=1"
    )
    if not runs.workflow_runs:
        raise ReleaseError("no successful main publication is available")
    run = runs.workflow_runs[0]
    if (
        run.path,
        run.event,
        run.head_branch,
        run.status,
        run.conclusion,
        run.repository.full_name,
    ) != (
        ".github/workflows/images.yml",
        "push",
        "main",
        "completed",
        "success",
        REPOSITORY,
    ):
        raise ReleaseError("release does not identify a successful Images/main/push attempt")
    jobs = _get(_Jobs, f"actions/runs/{run.id}/attempts/{run.run_attempt}/jobs?per_page=100")
    publish = [job for job in jobs.jobs if job.name == "publish"]
    if jobs.total_count > 100 or len(publish) != 1 or publish[0].conclusion != "success":
        raise ReleaseError("publication did not succeed exactly once")
    for name in (
        "Build serve candidate with attestations",
        "Build worker candidate with attestations",
        "Pull and verify exact candidate digests",
        "Tag verified manifests without rebuilding",
    ):
        steps = [step for step in publish[0].steps if step.name == name]
        if len(steps) != 1 or steps[0].conclusion != "success":
            raise ReleaseError("required publication verification step did not succeed")
    if _get(_Ref, "git/ref/heads/main").object.sha != run.head_sha:
        raise ReleaseError("release is no longer the main branch head")
    return run.head_sha
