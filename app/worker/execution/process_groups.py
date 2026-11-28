"""POSIX process groups owned by a CLI started in a fresh session."""

import os
import signal
import subprocess

from loguru import logger


def process_groups(pid: int) -> set[int]:
    """Snapshot before signalling: orphaned shells otherwise lose their parent identity."""
    try:
        snapshot = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True,
            check=True, timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("CLI descendant snapshot unavailable; cleanup may leave detached processes")
        return {pid}
    rows = [tuple(map(int, row.split())) for row in snapshot.stdout.splitlines()]
    descendants = {pid}
    while True:
        children = {child for child, parent, _ in rows if parent in descendants}
        if children <= descendants:
            break
        descendants.update(children)
    return {pid} | {group for child, _, group in rows if child in descendants
                    and group > 1 and group != os.getpgrp()}


def signal_groups(groups: set[int], sig: signal.Signals) -> None:
    """Signal each owned group, tolerating a group that already exited."""
    for group in groups:
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            pass
