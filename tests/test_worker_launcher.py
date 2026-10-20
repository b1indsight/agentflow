"""Real harmless subprocesses and final-path venvs, without registering a LaunchAgent."""

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from app.worker.updates.state import PreparedRelease, write_json
from tests.test_worker_updates import request

LAUNCHER = Path(__file__).parents[1] / "scripts/worker_update/launcher.py"


@pytest.mark.parametrize("crash", [False, True])
def test_launcher_waits_for_old_exit_and_consumes_one_cutover_start(tmp_path, crash):
    # These tiny installed programs stand in for Worker processes. Both use the
    # same root lock and late import, exposing double-start and mutable-import bugs.
    worker = tmp_path / "worker"
    worker.mkdir()
    journal = worker / "assignment-journal.json"
    journal.write_bytes(b"preserved journal")
    pointers = []
    for index, name in enumerate(("old", "new"), 1):
        path = tmp_path / "releases" / name
        venv.EnvBuilder(with_pip=False, symlinks=True).create(path / ".venv")
        library = (
            path
            / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/app"
        )
        library.mkdir()
        (library / "__init__.py").write_text("")
        (library / "late.py").write_text(f"VERSION = {name!r}\n")
        (library / "cli.py").write_text(
            "import fcntl,json,os\nfrom pathlib import Path\n"
            "root=Path(os.environ['FLOWTOOL_WORKER_UPDATE_ROOT'])\n"
            "lock=(root/'worker/.worker.lock').open('a')\n"
            "fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            f"version={name!r}\n"
            "if version=='old':\n"
            "    pointer=json.loads((root/'next.json').read_text())\n"
            "    state={'phase':'cutover','operation_id':'a'*32,'prepared':pointer}\n"
            "    (root/'operation.json').write_text(json.dumps(state))\n"
            "    (root/'current.tmp').write_text(json.dumps(pointer))\n"
            "    os.replace(root/'current.tmp',root/'current.json')\n"
            "from app.late import VERSION\n"
            "with (root/'trace.jsonl').open('a') as trace:\n"
            "    trace.write(json.dumps({'version':version,'late':VERSION,'pid':os.getpid()})+'\\n')\n"
            "if version=='new':\n"
            f"    if {crash!r}: raise SystemExit(7)\n"
            "    state=json.loads((root/'operation.json').read_text())\n"
            "    state['phase']='complete'\n"
            "    (root/'operation.json').write_text(json.dumps(state))\n"
        )
        release = request().release.model_copy(update={"sha": str(index) * 40})
        pointer = PreparedRelease(path=path, release=release)
        write_json(path / "complete.json", pointer.model_dump(mode="json"))
        pointers.append(pointer)
    write_json(tmp_path / "current.json", pointers[0].model_dump(mode="json"))
    write_json(tmp_path / "next.json", pointers[1].model_dump(mode="json"))
    write_json(
        tmp_path / "environment.json", {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    )
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == (7 if crash else 0), result.stderr
    traces = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert [entry["version"] for entry in traces] == ["old", "new"]
    assert [entry["late"] for entry in traces] == ["old", "new"]
    assert traces[0]["pid"] != traces[1]["pid"]
    assert journal.read_bytes() == b"preserved journal"
    assert (tmp_path / "startups" / ("a" * 32)).exists()
    if crash:
        again = subprocess.run(
            [sys.executable, str(LAUNCHER), "--root", str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert again.returncode == 1
        assert len((tmp_path / "trace.jsonl").read_text().splitlines()) == 2
