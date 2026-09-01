from pathlib import Path

import pytest

from app.worker.runtime import WorkerRootLock


def test_worker_root_lock_prevents_a_second_process_root_owner(tmp_path: Path) -> None:
    first = WorkerRootLock(tmp_path)
    second = WorkerRootLock(tmp_path)

    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
