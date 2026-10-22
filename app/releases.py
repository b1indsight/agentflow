"""Read-only verification of the fixed public Images/main publication contract."""

from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.contracts.protocol_base import ProtocolModel

REPOSITORY = "b1indsight/agentflow"
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class ReleaseEvent(ProtocolModel):
    """Verified publication metadata used locally, never a Worker protocol message."""

    version: Literal[1] = 1
    repository: Literal["b1indsight/agentflow"] = "b1indsight/agentflow"
    sha: Sha
    run_id: int = Field(gt=0, le=10**18)
    attempt: int = Field(gt=0, le=10**9)
    run_number: int = Field(gt=0, le=10**18)


class ReleaseError(RuntimeError):
    """A public release cannot be verified; diagnostics never contain credentials."""


class _Repository(BaseModel):
    full_name: str


class _Run(BaseModel):
    id: int
    run_attempt: int
    run_number: int
    head_sha: str
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


def verify_release(release: ReleaseEvent) -> ReleaseEvent:
    """Require the exact successful push attempt, publication steps and current main SHA."""
    run = _get(_Run, f"actions/runs/{release.run_id}")
    if (
        run.id,
        run.run_attempt,
        run.run_number,
        run.head_sha,
        run.path,
        run.event,
        run.head_branch,
        run.status,
        run.conclusion,
        run.repository.full_name,
    ) != (
        release.run_id,
        release.attempt,
        release.run_number,
        release.sha,
        ".github/workflows/images.yml",
        "push",
        "main",
        "completed",
        "success",
        REPOSITORY,
    ):
        raise ReleaseError("release does not identify a successful Images/main/push attempt")
    jobs = _get(
        _Jobs, f"actions/runs/{release.run_id}/attempts/{release.attempt}/jobs?per_page=100"
    )
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
    if _get(_Ref, "git/ref/heads/main").object.sha != release.sha:
        raise ReleaseError("release is no longer the main branch head")
    return release


def latest_release() -> ReleaseEvent:
    """Read the latest verified publication independently of Server deployment state."""
    runs = _get(
        _Runs, "actions/workflows/images.yml/runs?branch=main&event=push&status=success&per_page=1"
    )
    if not runs.workflow_runs:
        raise ReleaseError("no successful main publication is available")
    run = runs.workflow_runs[0]
    return verify_release(
        ReleaseEvent(
            sha=run.head_sha, run_id=run.id, attempt=run.run_attempt, run_number=run.run_number
        )
    )


def release_for_sha(sha: str) -> ReleaseEvent:
    """Resolve a local CLI request using verified metadata, never caller-supplied run claims."""
    release = latest_release()
    if release.sha != sha:
        raise ReleaseError("SHA does not match the latest successful main publication")
    return release
