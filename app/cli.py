"""Admin CLI（typer）。见 docs/architecture/decisions.md §10-8。

serve：起 webhook 常驻进程；flows/show/cancel/retry：运维 flow。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import typer

from app.config import get_settings
from app.models import FlowState, JobKind
from app.store import Store

app = typer.Typer(help="FlowTool admin CLI")
flow_app = typer.Typer(help="flow 运维")
app.add_typer(flow_app, name="flow")


@contextmanager
def _store() -> Iterator[Store]:
    store = Store(get_settings().database_url)
    store.init_db()
    try:
        yield store
    finally:
        store.close()


@app.command()
def serve() -> None:
    """启动 webhook 常驻进程（uvicorn）。"""
    import uvicorn

    s = get_settings()
    uvicorn.run("app.ingress:app", host=s.host, port=s.port, log_level="info")


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
        f.state = FlowState.CANCELLED
        store.update_flow(f)
    typer.echo(f"flow {flow_id} cancelled")


@flow_app.command("retry")
def flow_retry(flow_id: int) -> None:
    """重新排一个 initial job（失败恢复用）。"""
    with _store() as store:
        f = store.get_flow(flow_id)
        if not f:
            raise typer.Exit(1)
        store.enqueue_job(flow_id, JobKind.INITIAL, payload={"issue_number": f.issue_number})
    typer.echo(f"re-enqueued initial job for flow {flow_id}")


if __name__ == "__main__":
    app()
