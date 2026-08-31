"""Admin CLI（typer）。见 docs/architecture/decisions.md §10-8。

serve：起 webhook 常驻进程；flows/show/cancel/retry：运维 flow。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import typer

from app.config import get_serve_settings
from app.control.service import ControlService
from app.store import Store

app = typer.Typer(help="FlowTool admin CLI")
flow_app = typer.Typer(help="flow 运维")
app.add_typer(flow_app, name="flow")


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
            pr = f"PR#{att.pr_number}" if att and att.pr_number else "-"
            cycles = att.revision_cycles if att else 0
            typer.echo(
                f"[{f.id}] {f.repo}#{f.issue_number:<5} {f.state:<10} {pr} cycles={cycles}"
            )


@flow_app.command("show")
def flow_show(flow_id: int) -> None:
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        typer.echo(f"flow {f.id}\n  repo={f.repo} issue=#{f.issue_number} state={f.state}")
        for att in store.list_attempts(f.id):
            cur = " (current)" if att.id == f.current_attempt_id else ""
            typer.echo(
                f"  attempt {att.id}{cur}: branch={att.branch} pr={att.pr_number}"
                f" session={att.codex_session_id} cycles={att.revision_cycles}"
            )


@flow_app.command("cancel")
def flow_cancel(flow_id: int) -> None:
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(store).cancel(f)
    typer.echo(f"flow {flow_id} cancelled")


@flow_app.command("retry")
def flow_retry(flow_id: int) -> None:
    """重新排入最近失败的 job（没有失败记录时使用 initial）。"""
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        ControlService(store).retry(f)
    typer.echo(f"re-enqueued initial job for flow {flow_id}")


if __name__ == "__main__":
    app()
