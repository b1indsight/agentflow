"""Admin CLI（typer）。见 docs/architecture/decisions.md §10-8。

serve：起 webhook 常驻进程；flows/show/cancel/retry：运维 flow。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
import uvicorn
from pydantic import ValidationError

from app import github
from app.domain.models import ArchiveReason, DeliveryState
from app.worker.cli import app as worker_app

if TYPE_CHECKING:
    from app.persistence.store import Store

app = typer.Typer(help="FlowTool admin CLI")
app.add_typer(worker_app, name="worker")
flow_app = typer.Typer(help="flow 运维")
app.add_typer(flow_app, name="flow")
project_app = typer.Typer(help="Projects v2 运维")
app.add_typer(project_app, name="project")


inbox_app = typer.Typer(help="durable webhook 运维")
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("status")
def inbox_status(limit: int = typer.Option(50, min=1, max=500)) -> None:
    """Show bounded delivery diagnostics without payload or authentication data."""

    with _store() as store:
        for item in store.webhooks.list(limit):
            typer.echo(f"{item.delivery_id} event={item.event_type} state={item.state} "
                       f"attempts={item.attempts} next={item.next_at.isoformat()} error={item.error_code or '-'}")


@inbox_app.command("retry")
def inbox_retry(delivery_id: str) -> None:
    """Requeue only a blocked receipt; never reset a live claim or completed event."""

    with _store() as store:
        if not store.webhooks.retry(delivery_id):
            typer.echo("delivery is not blocked or has no retained payload", err=True)
            raise typer.Exit(1)
    typer.echo(f"delivery {delivery_id} queued")


@project_app.command("check")
def project_check(repo: str) -> None:
    """只读校验已配置的组织 Project、Status 字段及选项。"""
    from app.controler.config import ServerSettings

    settings = ServerSettings()  # type: ignore[call-arg]
    config = settings.projects.get(repo.casefold())
    if config is None:
        typer.echo(f"{repo}: Projects disabled")
        return
    try:
        installation = github.GitHubApp(settings).installation(config.installation_id)
        target = asyncio.run(installation.project(config).validate(installation.repository(repo)))
    except github.GitHubError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{repo}: {target.title} ({config.project_id}), Status={target.status_name} ({config.status_field_id})")


@project_app.command("status")
def project_status(repo: str, limit: int = typer.Option(100, min=1, max=1000)) -> None:
    """显示持久投影、期望/确认版本、退避和阻塞原因。"""

    with _store() as store:
        for row in store.projects.list_projections(repo=repo.casefold(), limit=limit):
            typer.echo(f"{row.repo}#{row.issue_number} flow={row.flow_id} project={row.project_id} "
                       f"item={row.item_id or '-'} {row.desired_status} "
                       f"version={row.synced_version}/{row.desired_version} blocked={row.blocked} "
                       f"next={row.next_at.isoformat()} error={row.last_error or '-'}")


@project_app.command("retry")
def project_retry(repo: str) -> None:
    """修复配置或远端条件后重新核对；不清除已知卡片或未确认的 add。"""

    with _store() as store:
        count = store.projects.retry(repo)
    typer.echo(f"{count} project projections scheduled for recheck")


@contextmanager
def _store() -> Iterator[Store]:
    from app.controler.config import ServerSettings
    from app.persistence.store import Store

    settings = ServerSettings()  # type: ignore[call-arg]
    store = Store(settings.database_url)
    store.init_db()
    try:
        yield store
    finally:
        store.close()


@app.command()
def serve() -> None:
    """启动 webhook 常驻进程（uvicorn）。"""
    from app.controler.api import create_app
    from app.controler.config import ServerSettings
    from app.controler.runtime import ServerRuntime

    settings = ServerSettings()  # type: ignore[call-arg]
    uvicorn.run(
        create_app(ServerRuntime(settings)),
        host=settings.host,
        port=settings.port,
        log_level="info",
        log_config=None,  # Keep the runtime's sanitized handlers when Uvicorn initializes.
        timeout_graceful_shutdown=20,
    )


@app.command()
def flows(active: bool = typer.Option(False, help="只看未终结的")) -> None:
    """列出 flows。"""
    with _store() as store:
        for f in store.flows.list_flows(active_only=active):
            att = store.flows.get_current_attempt(f)
            delivery = store.deliveries.get_delivery(f.id)
            pr = f"PR#{delivery.pr_number}" if delivery and delivery.pr_number else "-"
            cycles = att.revision_cycles if att else 0
            target = f"{f.repo}#{f.issue_number}" if f.issue_number is not None else f.repo
            typer.echo(f"[{f.id}] {target} {f.state:<10} {pr} cycles={cycles}")


@flow_app.command("create")
def flow_create(
    repo: str,
    installation_id: int = typer.Option(..., min=1),
    issue_number: int | None = typer.Option(None, min=1),
    content: str | None = typer.Option(None),
    content_file: Annotated[Path | None, typer.Option(help="UTF-8 任务内容文件，- 表示 stdin")] = None,
) -> None:
    """从 Issue 或直接任务内容创建 Flow；不要求 webhook。"""
    from app.contracts.console import CreateFlowRequest
    from app.controler.config import ServerSettings
    from app.controler.workflow.service import ControlService

    try:
        if content_file is not None:
            if content is not None:
                raise typer.BadParameter("provide only one of --content and --content-file")
            if str(content_file) == "-":
                content = sys.stdin.read(32769)
            else:
                with content_file.open(encoding="utf-8", newline="") as stream:
                    content = stream.read(32769)
        request = CreateFlowRequest(
            repo=repo, installation_id=installation_id, issue_number=issue_number,
            task_content=content,
        )
    except (OSError, UnicodeError) as error:
        raise typer.BadParameter("cannot read UTF-8 task content") from error
    except ValidationError as error:
        raise typer.BadParameter(error.errors(include_input=False)[0]["msg"]) from error
    settings = ServerSettings()  # type: ignore[call-arg]

    async def target() -> github.Repository:
        # Same read-only target checks as console/api.py; never hold a DB transaction over I/O.
        repository = await github.GitHubApp(settings).installation(request.installation_id).repository(request.repo).refetch()
        if repository.archived:
            raise github.GitHubError(github.GitHubErrorKind.VALIDATION, "repository_archived")
        if request.issue_number is not None:
            await repository.fetch_issue(request.issue_number)
        return repository

    try:
        repository = asyncio.run(target())
    except github.GitHubError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    with _store() as store:
        flow = ControlService(database=store.database).create_flow(
            repository.full_name, request.issue_number, request.installation_id,
            task_content=request.task_content,
        )
        if flow is None:
            typer.echo("an active Flow already exists for this Issue", err=True)
            raise typer.Exit(1)
        typer.echo(f"flow {flow.id} created (queued) for {flow.repo}")


@app.command("worker-drain")
def worker_drain(worker_id: str, undo: bool = typer.Option(False, "--undo")) -> None:
    """暂停给 worker 分配新 Flow；已有绑定继续收尾。"""
    with _store() as store:
        store.workers.drain(worker_id, not undo)
    typer.echo(f"{worker_id}: draining={not undo}")


@app.command()
def workers() -> None:
    """列出 worker 在线状态和当前 assignment。"""
    from app.controler.workflow.service import ControlService

    with _store() as store:
        typer.echo("WORKER\tSTATUS\tDRAINING\tEPOCH\tLAST_SEEN\tREASON")
        for worker in ControlService(
            database=store.database,
        ).list_workers():
            typer.echo(
                f"{worker.worker_id}\t{worker.status.value}\t{worker.draining}\t"
                f"{(worker.connection_epoch or '-')[:8]}\t"
                f"{_age(worker.last_seen_at)}\t"
                f"{worker.last_disconnect_reason or '-'}"
            )
            bindings = {item.workspace: item for item in worker.executions}
            for workspace in worker.workspaces:
                binding = bindings.get(workspace.workspace)
                typer.echo(
                    f"  {workspace.workspace}\tgeneration={workspace.generation}"
                    f"\trepo={workspace.repository or '-'}"
                    f"\tjob={workspace.current_job_id or workspace.current_inference_job_id or '-'}"
                    f"\tflow={binding.flow_id if binding else '-'}"
                )


def _age(value: datetime) -> str:
    seconds = max(0, int((datetime.now(UTC) - value).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _remaining(value: datetime | None) -> str:
    if value is None:
        return "-"
    return f"{max(0, int((value - datetime.now(UTC)).total_seconds()))}s"


@flow_app.command("show")
def flow_show(flow_id: int) -> None:
    from app.controler.workflow.archive import archive_progress

    with _store() as store:
        f = store.flows.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        typer.echo(
            f"flow {f.id}\n  repo={f.repo} issue={f'#{f.issue_number}' if f.issue_number is not None else '-'} state={f.state}"
            f" round={f.current_round_no} outcome={f.archive_reason or '-'}"
            f" archived_at={f.archived_at or '-'}"
        )
        if f.task_content is not None:
            typer.echo(f"  task:\n{f.task_content}")
        execution = store.workers.get_flow_execution(f.id)
        delivery = store.deliveries.get_delivery(f.id)
        if execution is not None:
            typer.echo(
                f"  execution: worker={execution.worker_id} repo={execution.repo_identity}"
                f" workspace={execution.workspace} generation={execution.generation}"
                f" active={execution.released_at is None}"
            )
        if delivery is not None:
            typer.echo(
                f"  delivery: state={delivery.state} branch={delivery.branch}"
                f" pr={delivery.pr_number or '-'} head={delivery.head_sha or '-'}"
            )
            if delivery.state is DeliveryState.CLOSED:
                typer.echo(
                    "  closed: finalization or baseline reconciliation required;"
                    " reopening does not authorize work"
                )
        check = store.reconcile.get(f.id)
        if check is not None:
            typer.echo(f"  reconcile: last={check.last_success_at or '-'} next={check.next_at}"
                       f" failures={check.failures} reason={check.last_error or '-'}")
        for source in ("issue_comments", "pr_comments", "reviews", "threads"):
            observation = store.reconcile.feedback_get(f.id, source)
            if observation is not None:
                typer.echo(f"  feedback {source}: cursor={observation.cursor or '-'}"
                           f" last={observation.last_success_at or '-'} next={observation.next_at}"
                           f" failures={observation.failures} reason={observation.last_error or '-'}")
        for att in store.flows.list_attempts(f.id):
            cur = " (current)" if att.id == f.current_attempt_id else ""
            typer.echo(
                f"  attempt {att.id}{cur}: session={att.codex_session_id}"
                f" cycles={att.revision_cycles}"
            )
        jobs = store.jobs.list_jobs(f.id)
        if delivery is not None and delivery.state is DeliveryState.MERGED:
            typer.echo(
                f"  archive: {archive_progress(f, jobs, result_store=store.results, workers=store.workers)}"
            )
        for job in jobs:
            recovery = _remaining(job.recovery_deadline)
            result = "received" if job.result_received_at is not None else "-"
            typer.echo(
                f"  job {job.id}: kind={job.kind} state={job.state} "
                f"worker={job.assigned_worker_id or '-'} recovery={recovery} result={result}"
            )
            if job.last_error:
                typer.echo(f"    error: {job.last_error}")
            if job.prepare_failures:
                typer.echo(f"    preparation: failures={job.prepare_failures}"
                           f" first={job.prepare_first_failed_at} next={job.prepare_next_at or '-'}"
                           f" reason={job.prepare_last_error or '-'}")
            if job.verify_next_at:
                typer.echo(f"    verification: failures={job.verify_failures} next={job.verify_next_at}"
                           f" reason={job.verify_last_error or '-'}")
            for effect in store.results.list_effects(job.id):
                if effect.state.value not in {"succeeded", "skipped"}:
                    typer.echo(
                        f"    effect {effect.effect_key}: {effect.state} "
                        f"next={effect.next_attempt_at.isoformat()} reason={effect.last_error or '-'}"
                    )


@flow_app.command("cancel")
def flow_cancel(flow_id: int) -> None:
    from app.controler.workflow.service import ControlService

    with _store() as store:
        f = store.flows.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(
            database=store.database,
        ).cancel(f)
        waiting = store.stops.has_pending(flow_id)
    typer.echo(f"flow {flow_id}: cancellation requested; stop_pending={waiting}")


@flow_app.command("retry")
def flow_retry(flow_id: int) -> None:
    """重试失败任务，或补发已核验修订中被明确拒绝的 GitHub 通知。"""
    from app.controler.workflow.service import ControlService

    with _store() as store:
        f = store.flows.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(
            database=store.database,
        ).retry(f)
    typer.echo(f"retry requested for flow {flow_id}")


@flow_app.command("finish")
def flow_finish(
    flow_id: int,
    reason: ArchiveReason,
    summary: str = typer.Option(""),
    expected_round: int = typer.Option(..., min=1),
) -> None:
    """结束指定轮次；无 PR 的成功调查必须提供结论。"""
    from app.controler.workflow.service import ControlService

    with _store() as store:
        flow = store.flows.get_flow(flow_id)
        if flow is None:
            raise typer.Exit(1)
        try:
            ControlService(database=store.database).finish(
                flow, reason=reason, summary=summary, expected_round_no=expected_round
            )
        except ValueError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from None
    typer.echo(f"finalization requested for flow {flow_id} round {expected_round}")


@flow_app.command("resume")
def flow_resume(flow_id: int, expected_round: int = typer.Option(..., min=1)) -> None:
    """显式恢复已归档 Flow，开启新轮次和新交付。"""
    from app.controler.workflow.service import ControlService

    with _store() as store:
        flow = store.flows.get_flow(flow_id)
        if flow is None:
            raise typer.Exit(1)
        try:
            resumed = ControlService(database=store.database).resume(
                flow, expected_round_no=expected_round
            )
        except ValueError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(1) from None
    typer.echo(f"flow {flow_id} round {resumed.current_round_no}: queued")


@flow_app.command("history")
def flow_history(flow_id: int, round_no: int = typer.Option(..., "--round", min=1)) -> None:
    """按轮次显示状态、归档、合并证据与自动更正的原始活动记录。"""
    from app.persistence.activity import ActivityStore

    with _store() as store:
        flow = store.flows.get_flow(flow_id)
        if flow is None or round_no > flow.current_round_no:
            raise typer.Exit(1)
        for item in ActivityStore(store.database).history(flow_id, round_no):
            typer.echo(item.model_dump_json())


if __name__ == "__main__":
    app()
