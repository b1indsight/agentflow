"""Compare a Git baseline with the real working-tree implementation, without migration.

Run with FLOWTOOL_TEST_DATABASE_URL pointing to a disposable local PostgreSQL:
    uv run python scripts/smoke/control_legacy_demo.py --before <base-commit>

The same scenarios run in separate processes against an exported baseline and
the working tree. Existing fixtures isolate PostgreSQL schemas and fake GitHub.
Run pytest directly on this file to check only the after version.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from app.contracts.protocol import InferenceResult, result_header
from app.contracts.result_models import CommentEffect, EffectState, ResultPlan
from app.controler.workflow.coordinator import JobCoordinator
from app.controler.workflow.service import ControlService
from app.domain.inference_models import (
    InferenceCapability,
    InferenceMessage,
    InferenceOutput,
    InferenceTask,
)
from app.domain.models import FlowState, JobKind, JobState, TriageVerdict, Verdict
from tests.github import RecordingGitHub
from tests.test_coordinator import accept_result, coordinator
from tests.test_review_batches import claim, feedback, ingest, run_triage
from tests.workspace_fixtures import register_worker

pytest_plugins = ["tests.conftest", "tests.test_review_batches", "tests.test_result_recovery"]
pytestmark = pytest.mark.usefixtures("recording_resources")


@pytest.fixture
def phase():
    """Choose assertions for the source version selected by the comparison runner."""
    return os.environ.get("CONTROL_LEGACY_DEMO_PHASE", "after")


async def test_answer(phase, store, review_flow):
    flow, clock = review_flow
    ingest(store, flow, feedback(clock, body="Why is the retry limit ten?"))
    batch = claim(store, clock)[0]
    _, github, _ = await run_triage(store, flow, batch, TriageVerdict(
        verdict=Verdict.ANSWER, instruction="Explain why the retry limit is ten.",
    ))
    jobs = store.jobs.list_jobs(flow.id)
    assert len(jobs) == 1 and jobs[0].kind is JobKind.REVISION
    assert jobs[0].payload["purpose"] == "answer" and not github.comments
    legacy = hasattr(JobCoordinator, "_answer")
    assert legacy is (phase == "before")
    print(f"\n{phase:6} answer: revision_jobs={len(jobs)}, controller_comments=0, legacy_handler={legacy}")


async def test_legacy_failure_comment(phase, scenario):
    # Synthetic old data illustrates the removed suppression; it is not migrated.
    store, github, job, result = scenario
    subject = coordinator(store, github)
    accept_result(subject, job, result.model_copy(update={"exit_code": 65, "output": None}))
    saved = store.jobs.get_job(job.id)
    store.results.prepare(ResultPlan(
        job_id=job.id, flow_id=job.flow_id, digest=saved.result_digest,
        attempt_id=store.flows.get_flow(job.flow_id).current_attempt_id,
        state=JobState.FAILED, flow_state=FlowState.BLOCKED, error="demo failure",
        effects=[CommentEffect(key="failure", installation_id=42, repo="owner/repo",
                               number=17, body="Synthetic legacy failure")],
    ))
    assert await subject.results.apply(job.id) is JobState.FAILED
    effect, = store.results.list_effects(job.id)
    expected = EffectState.SKIPPED if phase == "before" else EffectState.SUCCEEDED
    assert effect.state is expected
    assert len(github.comments) == (0 if phase == "before" else 1)
    print(f"\n{phase:6} synthetic failure: effect={effect.state.value}, fake_comments={len(github.comments)}")


@pytest.mark.parametrize("validation", ["text", "flow_naming"])
async def test_title(phase, validation, store):
    flow = ControlService(database=store.database).create_flow(
        "owner/repo", None, 42, task_content="Fix pagination cursor",
    )
    naming = validation == "flow_naming"
    text = json.dumps({"title": "修复分页游标", "branch_slug": "fix/cursor"}) if naming else "普通推理结果"
    job = store.jobs.enqueue_inference(InferenceTask(
        messages=[InferenceMessage(role="user", content="Return the demo result")],
    ), flow_id=flow.id, validation=validation)
    with store.connection() as connection:
        connection.execute("UPDATE flows SET title_job_id=%s WHERE id=%s", (job.id, flow.id))
    register_worker(store, "demo-worker", epoch="demo-epoch")
    store.workers.worker_ready("demo-worker", "demo-epoch", InferenceCapability(adapter_version="codex/v1"))
    assigned = store.workers.assign_job("demo-worker", "demo-token", "demo-epoch", slot="infer")
    assert assigned is not None and assigned.id == job.id
    call = assigned.inference.current_task
    response = InferenceResult(
        **result_header(call.request_id), method="worker.infer", job_id=job.id,
        assignment_token="demo-token", output=InferenceOutput(
            request_id=call.request_id, request_digest=call.digest,
            adapter_version="codex/v1", text=text,
        ),
    )
    assert store.workers.record_assignment_result(
        job.id, "demo-worker", "demo-token", "demo-epoch", response,
    ) == "pending"
    assert await coordinator(store, RecordingGitHub()).results.apply(job.id) is JobState.DONE
    title = store.flows.get_flow(flow.id).title
    assert title == ("修复分页游标" if naming else "普通推理结果" if phase == "before" else None)
    assert store.jobs.get_job(job.id).result_payload["output"]["text"] == text
    print(f"\n{phase:6} {validation}: state=done, title={title!r}, output_preserved=True")


def main():
    """Run identical scenarios on an exported baseline and the untouched working tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="Git commit before the cleanup")
    args = parser.parse_args()
    if not os.environ.get("FLOWTOOL_TEST_DATABASE_URL"):
        parser.error("FLOWTOOL_TEST_DATABASE_URL must point to a disposable local PostgreSQL")
    root = Path(__file__).resolve().parents[2]
    relative = Path("scripts/smoke/control_legacy_demo.py")
    with TemporaryDirectory(prefix="control-legacy-demo-") as temporary:
        baseline = Path(temporary) / "before"
        baseline.mkdir()
        archive = Path(temporary) / "before.tar"
        subprocess.run(["git", "archive", "--output", str(archive), args.before], cwd=root, check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(baseline)], check=True)
        shutil.copy2(__file__, baseline / relative)
        for version, directory in [("before", baseline), ("after", root)]:
            print(f"\n=== {version}: {directory} ===", flush=True)
            subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-s", str(relative)], cwd=directory,
                env=os.environ | {"CONTROL_LEGACY_DEMO_PHASE": version, "PYTHONPATH": str(directory)},
                check=True,
            )


if __name__ == "__main__":
    main()
