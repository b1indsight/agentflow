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


def test_distinct_worker_roots_record_identity_and_lock_independently(tmp_path: Path) -> None:
    first_root = tmp_path / "worker-a"
    second_root = tmp_path / "worker-b"
    first = WorkerRootLock(first_root, "worker-a")
    second = WorkerRootLock(second_root, "worker-b")

    first.acquire()
    second.acquire()
    try:
        assert (first_root / ".worker-id").read_text() == "worker-a\n"
        assert (second_root / ".worker-id").read_text() == "worker-b\n"
    finally:
        first.release()
        second.release()


def test_worker_root_rejects_sequential_identity_reuse(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    first = WorkerRootLock(root, "worker-a")
    first.acquire()
    first.release()

    with pytest.raises(RuntimeError, match="belongs to worker-a"):
        WorkerRootLock(root, "worker-b").acquire()
