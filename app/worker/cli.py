"""Worker process and explicit offline release preparation commands."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError

from app import releases

app = typer.Typer(help="Worker execution and manual updates", invoke_without_command=True)


@app.callback()
def worker(context: typer.Context) -> None:
    """Start the Worker when no subcommand is supplied."""
    if context.invoked_subcommand is not None:
        return
    from app.worker.config import settings
    from app.worker.runtime import WorkerRuntime

    asyncio.run(WorkerRuntime(settings).run())


@app.command()
def update(
    sha: Annotated[
        str, typer.Option(help="Full lowercase SHA of the verified Images/main publication")
    ],
    root: Annotated[
        Path | None, typer.Option(help="Directory for prepared releases; requires --apply")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Prepare a release after manually stopping the Worker")
    ] = False,
) -> None:
    """Check a release, or prepare it for an explicit operator restart without switching processes."""
    try:
        TypeAdapter(releases.Sha).validate_python(sha)
    except ValidationError:
        raise typer.BadParameter(
            "expected 40 lowercase hexadecimal characters", param_hint="--sha"
        ) from None
    if apply != (root is not None):
        raise typer.BadParameter("--apply and --root must be supplied together")
    try:
        payload: dict[str, object]
        if root is None:
            publication = releases.release_for_sha(sha)
            payload = {"state": "checked", **publication.model_dump(mode="json")}
        else:
            from app.worker.config import settings
            from app.worker.runtime import WorkerRootLock
            from app.worker.state.session import WorkerSession
            from app.worker.updates.release import prepare

            if not (settings.worker_root / ".worker-id").is_file():
                raise RuntimeError("configured Worker root has no recorded identity")
            lock = WorkerRootLock(settings.worker_root, settings.worker_id)
            lock.acquire()
            try:
                record = WorkerSession(settings.worker_root).load()
                if record is not None and not record.acknowledged:
                    raise RuntimeError("Worker journal needs ordinary recovery before updating")
                prepared = prepare(root.expanduser().resolve(), releases.release_for_sha(sha))
            finally:
                lock.release()
            payload = {
                "state": "prepared",
                **prepared.model_dump(mode="json"),
                "python": str(prepared.path / ".venv/bin/python"),
            }
        typer.echo(json.dumps(payload, sort_keys=True))
    except RuntimeError as error:
        typer.echo(f"worker update stopped: {error}", err=True)
        raise typer.Exit(1) from None
    except (OSError, ValueError) as error:
        # Validation and filesystem errors must not echo credentials or package output.
        typer.echo(f"worker update stopped: {type(error).__name__}", err=True)
        raise typer.Exit(1) from None
