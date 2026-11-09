"""Explicit simulated worker inventory for persistence tests, never a product fallback."""

from uuid import NAMESPACE_URL, uuid5

from app.contracts.protocol import PROTOCOL_VERSION
from app.domain.models import WorkerRecord, WorkspaceInventory
from app.persistence.records import required
from app.persistence.store import Store


def register_worker(
    store: Store, worker_id: str, *, repo: str | None = None, epoch: str = "",
) -> WorkerRecord:
    """Register a stable default directory and, when requested, one concrete repo slot."""
    worker = store.workers.worker_connected(
        worker_id, PROTOCOL_VERSION, epoch,
        workspace=uuid5(NAMESPACE_URL, worker_id + ":default").hex, repo_workspace_limit=2,
    )
    assert store.workers.report_workspace(worker_id, epoch, WorkspaceInventory(
        workspace=worker.workspace, repository=None, generation=1, state="idle", flow_id=None,
    ))
    if repo is not None:
        workspace = uuid5(NAMESPACE_URL, worker_id + ":" + repo).hex
        existing = next((w for w in store.workers.list_workspaces(worker_id) if w.workspace == workspace), None)
        binding = store.workers.get_worker_execution(worker_id, workspace)
        assert store.workers.report_workspace(worker_id, epoch, WorkspaceInventory(
            workspace=workspace, repository=repo, generation=existing.generation if existing else 1,
            state="occupied" if binding else "idle", flow_id=binding.flow_id if binding else None,
        ))
    return worker


def report_settled(store: Store, job_id: int, *, epoch: str = "", stopped: bool = False) -> None:
    """Simulate the worker receiving ACK before it reports the exact settled execution."""
    job = required(store.jobs.get_job(job_id))
    flow = required(store.flows.get_flow(job.required_flow_id))
    assert job.workspace is not None and job.generation is not None and job.assigned_worker_id is not None
    assert store.workers.report_workspace(job.assigned_worker_id, epoch, WorkspaceInventory(
        workspace=job.workspace, repository=flow.repo, generation=job.generation + int(stopped),
        state="idle", flow_id=None if stopped else flow.id,
        settled_job_id=job.id, settled_assignment_token=job.assignment_token,
    ))
