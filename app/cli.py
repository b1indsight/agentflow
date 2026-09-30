"""Admin CLI（typer）。见 docs/architecture/decisions.md §10-8。

serve：起 webhook 常驻进程；flows/show/cancel/retry：运维 flow。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import typer

from app.config import get_serve_settings
from app.control.archive import archive_progress
from app.control.service import ControlService
from app.domain.models import DeliveryState
from app.persistence.store import Store

app = typer.Typer(help="FlowTool admin CLI")
flow_app = typer.Typer(help="flow 运维")
app.add_typer(flow_app, name="flow")
project_app = typer.Typer(help="Projects v2 运维")
app.add_typer(project_app, name="project")


inbox_app = typer.Typer(help="durable webhook 运维")
app.add_typer(inbox_app, name="inbox")


@inbox_app.command("status")
def inbox_status(limit: int = typer.Option(50, min=1, max=500)) -> None:
    """Show bounded delivery diagnostics without payload or authentication data."""
    from app.persistence.webhook_store import WebhookStore

    with _store() as store:
        for item in WebhookStore(store).list(limit):
            typer.echo(f"{item.delivery_id} event={item.event_type} state={item.state} "
                       f"attempts={item.attempts} next={item.next_at.isoformat()} error={item.error_code or '-'}")


@inbox_app.command("retry")
def inbox_retry(delivery_id: str) -> None:
    """Requeue only a blocked receipt; never reset a live claim or completed event."""
    from app.persistence.webhook_store import WebhookStore

    with _store() as store:
        if not WebhookStore(store).retry(delivery_id):
            typer.echo("delivery is not blocked or has no retained payload", err=True)
            raise typer.Exit(1)
    typer.echo(f"delivery {delivery_id} queued")


@project_app.command("check")
def project_check(repo: str) -> None:
    """只读校验已配置的组织 Project、Status 字段及选项。"""
    from app.domain.github_models import GitHubAdapterError
    from app.github.client import GitHubApp

    settings = get_serve_settings()
    config = settings.projects.get(repo.casefold())
    if config is None:
        typer.echo(f"{repo}: Projects disabled")
        return
    try:
        target = asyncio.run(GitHubApp(settings).project_client(config).validate(repo))
    except GitHubAdapterError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{repo}: {target.title} ({config.project_id}), Status={target.status_name} ({config.status_field_id})")


@project_app.command("status")
def project_status(repo: str, limit: int = typer.Option(100, min=1, max=1000)) -> None:
    """显示持久投影、期望/确认版本、退避和阻塞原因。"""
    from app.persistence.project_store import ProjectStore

    with _store() as store:
        for row in ProjectStore(store).list_projections(repo=repo.casefold(), limit=limit):
            typer.echo(f"{row.repo}#{row.issue_number} flow={row.flow_id} project={row.project_id} "
                       f"item={row.item_id or '-'} {row.desired_status} "
                       f"version={row.synced_version}/{row.desired_version} blocked={row.blocked} "
                       f"next={row.next_at.isoformat()} error={row.last_error or '-'}")


@project_app.command("retry")
def project_retry(repo: str) -> None:
    """修复配置或远端条件后重新核对；不清除已知卡片或未确认的 add。"""
    from app.persistence.project_store import ProjectStore

    with _store() as store:
        count = ProjectStore(store).retry(repo)
    typer.echo(f"{count} project projections scheduled for recheck")


@contextmanager
def _store() -> Iterator[Store]:
    store = Store(get_serve_settings().database_url)
    store.init_db()
    try:
        yield store
    finally:
        store.close()


@app.command()
def serve() -> None:
    """启动 webhook 常驻进程（uvicorn）。"""
    import uvicorn

    settings = get_serve_settings()
    uvicorn.run(
        "app.control.api:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
        timeout_graceful_shutdown=20,
    )


@app.command()
def worker() -> None:
    """启动远程执行进程并主动连接 control plane。"""
    from app.worker.runtime import WorkerRuntime

    asyncio.run(WorkerRuntime().run())


@app.command()
def flows(active: bool = typer.Option(False, help="只看未终结的")) -> None:
    """列出 flows。"""
    with _store() as store:
        for f in store.list_flows(active_only=active):
            att = store.get_current_attempt(f)
            delivery = store.get_delivery(f.id)
            pr = f"PR#{delivery.pr_number}" if delivery and delivery.pr_number else "-"
            cycles = att.revision_cycles if att else 0
            typer.echo(f"[{f.id}] {f.repo}#{f.issue_number:<5} {f.state:<10} {pr} cycles={cycles}")


@app.command("worker-drain")
def worker_drain(worker_id: str, undo: bool = typer.Option(False, "--undo")) -> None:
    """暂停给 worker 分配新 Flow；已有绑定继续收尾。"""
    from app.persistence.stop_store import StopStore

    with _store() as store:
        StopStore(store).drain(worker_id, not undo)
    typer.echo(f"{worker_id}: draining={not undo}")


@app.command()
def workers() -> None:
    """列出 worker 在线状态和当前 assignment。"""
    with _store() as store:
        typer.echo("WORKER\tSTATUS\tDRAINING\tJOB\tFLOW\tEPOCH\tLEASE\tRECOVERY\tLAST_SEEN\tREASON")
        for worker in ControlService(store).list_workers():
            typer.echo(
                f"{worker.worker_id}\t{worker.status.value}\t{worker.draining}\t"
                f"{worker.current_job_id or '-'}\t{worker.current_flow_id or '-'}\t"
                f"{(worker.connection_epoch or '-')[:8]}\t"
                f"{_remaining(worker.lease_expires_at)}\t"
                f"{_remaining(worker.recovery_deadline)}\t"
                f"{_age(worker.last_seen_at)}\t"
                f"{worker.last_disconnect_reason or '-'}"
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
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        typer.echo(f"flow {f.id}\n  repo={f.repo} issue=#{f.issue_number} state={f.state}")
        execution = store.get_flow_execution(f.id)
        delivery = store.get_delivery(f.id)
        if execution is not None:
            typer.echo(
                f"  execution: worker={execution.worker_id} repo={execution.repo_identity}"
                f" active={execution.released_at is None}"
            )
        if delivery is not None:
            typer.echo(
                f"  delivery: state={delivery.state} branch={delivery.branch}"
                f" pr={delivery.pr_number or '-'} head={delivery.head_sha or '-'}"
            )
            if delivery.state is DeliveryState.CLOSED:
                typer.echo("  paused: original worker/repo retained; reopen requires settled execution"
                           " and verified head/base; flow retry cannot bypass this hold")
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
        for att in store.list_attempts(f.id):
            cur = " (current)" if att.id == f.current_attempt_id else ""
            typer.echo(
                f"  attempt {att.id}{cur}: session={att.codex_session_id}"
                f" cycles={att.revision_cycles}"
            )
        jobs = store.list_jobs(f.id)
        if delivery is not None and delivery.state is DeliveryState.MERGED:
            typer.echo(f"  archive: {archive_progress(store, f, jobs)}")
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
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(store).cancel(f)
        waiting = store.stops.has_pending(flow_id)
    typer.echo(f"flow {flow_id}: cancellation requested; stop_pending={waiting}")


@flow_app.command("retry")
def flow_retry(flow_id: int) -> None:
    """重试失败任务，或补发已核验修订中被明确拒绝的 GitHub 通知。"""
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(store).retry(f)
    typer.echo(f"retry requested for flow {flow_id}")


if __name__ == "__main__":
    app()
