"""Worker process and one-shot, in-place package update entry point."""

import asyncio
import json
import signal
from typing import Annotated

import typer

app = typer.Typer(help="Worker execution and package updates", invoke_without_command=True)


@app.callback()
def worker(
    update: Annotated[
        bool, typer.Option("--update", help="Update the installed package, verify it, then exit")
    ] = False,
) -> None:
    """Run the Worker, or update its existing installation without starting it."""
    from app.worker.self_update import UpdateError, installation_lock, update_worker

    try:
        if update:
            typer.echo(json.dumps(update_worker(), sort_keys=True))
            return
        with installation_lock(updating=False):
            from app.telemetry.logging import configure_logging
            from app.worker.config import settings
            from app.worker.runtime import WorkerRuntime

            configure_logging("worker", settings.log_format)
            runtime = WorkerRuntime(settings)
            with asyncio.Runner() as runner:
                runner.get_loop().add_signal_handler(signal.SIGTERM, runtime.stop)
                runner.run(runtime.run())
    except UpdateError as error:
        typer.echo(f"worker {'update' if update else 'startup'} stopped: {error}", err=True)
        raise typer.Exit(1) from None
