"""Public Worker CLI behavior, with installation writes confined to a temporary venv."""

import base64
import csv
import hashlib
import importlib.metadata
import inspect
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import venv
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from typer.testing import CliRunner

from app import cli

SHA = "2" * 40
OLD_SHA = "1" * 40
SOURCE = Path(__file__).resolve().parents[1]


def write_record(site):
    metadata = site / "flowtool-0.0.1.dist-info"
    paths = [*sorted((site / "app").rglob("*")), *sorted(metadata.iterdir())]
    paths.append(site.parents[2] / "bin" / "flowtool")
    with (metadata / "RECORD").open("w", newline="") as handle:
        writer = csv.writer(handle)
        for path in paths:
            if not path.is_file() or path.suffix == ".pyc" or path.name == "RECORD":
                continue
            body = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")
            writer.writerow([os.path.relpath(path, site), f"sha256={digest}", len(body)])
        writer.writerow(["flowtool-0.0.1.dist-info/RECORD", "", ""])


@pytest.fixture
def installation(tmp_path):
    # No install runs: borrow dependency code, and copy metadata for a real uv pip check.
    prefix = tmp_path / "installed"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(prefix)
    site = next((prefix / "lib").glob("python*/site-packages"))
    (site / "dependencies.pth").write_text(
        "import sys; sys.dont_write_bytecode = True\n" + sysconfig.get_path("purelib") + "\n"
    )
    for path in Path(sysconfig.get_path("purelib")).glob("*.dist-info"):
        if not path.name.startswith("flowtool-"):
            shutil.copytree(path, site / path.name)
    shutil.copytree(SOURCE / "app", site / "app", ignore=shutil.ignore_patterns("__pycache__"))
    (site / "app" / "__init__.py").write_text(f'INSTALLED_BODY = "{OLD_SHA}"\n')
    metadata = site / "flowtool-0.0.1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        importlib.metadata.distribution("flowtool").read_text("METADATA")
    )
    (metadata / "WHEEL").write_text(importlib.metadata.distribution("flowtool").read_text("WHEEL"))
    (metadata / "INSTALLER").write_text("uv\n")
    (metadata / "entry_points.txt").write_text("[console_scripts]\nflowtool = app.cli:app\n")
    (metadata / "direct_url.json").write_text(
        json.dumps(
            {
                "url": "https://github.com/b1indsight/agentflow",
                "vcs_info": {"vcs": "git", "commit_id": OLD_SHA},
            }
        )
    )
    entry = prefix / "bin" / "flowtool"
    entry.write_text(f"#!{prefix}/bin/python\nfrom app.cli import app\napp()\n")
    entry.chmod(0o755)
    write_record(site)
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\necho 'test must intercept uv' >&2\nexit 99\n")
    uv.chmod(0o755)
    return prefix, site


def invoke_installed(installation, *, fault="", arguments=("worker", "--update"), background=False):
    """Substitute only public GitHub and uv I/O; verification uses real fresh interpreters."""
    prefix, site = installation
    installer_wait = """import sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / "installer.ready").touch()
deadline = time.monotonic() + 15
while not (root / "installer.stop").exists() and time.monotonic() < deadline:
    time.sleep(.02)
"""
    code = f'''
import base64, csv, hashlib, importlib.abc, json, os, shutil, subprocess, sys
from pathlib import Path

class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("app.worker.config", "app.worker.runtime"):
            raise AssertionError("update/help must not import Worker credentials/runtime")
sys.meta_path.insert(0, NoRuntime())
from app import releases
from app.worker import self_update
import httpx

prefix = Path({str(prefix)!r})
site = Path({str(site)!r})
metadata = site / "flowtool-0.0.1.dist-info"
fault = {fault!r}
{inspect.getsource(write_record)}
if fault == "dependencies":
    path = metadata / "METADATA"
    path.write_text(path.read_text().replace("\\n\\n", "\\nRequires-Dist: flowtool-test-absent-dependency==1\\n\\n", 1))
    write_record(site)
if fault == "import-path":
    self_update.__file__ = str(prefix.parent / "unrelated" / "self_update.py")
if fault == "interpreter":
    sys.executable = "/different/bin/python"
def publication():
    if fault == "publication":
        raise releases.ReleaseError("publication unavailable")
    return releases.ReleaseEvent(sha={SHA!r}, run_id=1, attempt=1, run_number=1)
releases.latest_release = publication
def compare(request):
    assert request.url.path.endswith("/{OLD_SHA}...{SHA}")
    return httpx.Response(200, json={{
        "status": "behind" if fault == "downgrade" else "ahead",
        "base_commit": {{"sha": {OLD_SHA!r}}},
        "merge_base_commit": {{"sha": {OLD_SHA!r}}},
        "behind_by": 0,
    }})
client = httpx.Client(transport=httpx.MockTransport(compare))
httpx.stream = client.stream
real_run = subprocess.run
def run(command, **kwargs):
    if Path(command[0]).name != "uv":
        return real_run(command, **kwargs)
    with (prefix.parent / "commands.jsonl").open("a") as handle:
        handle.write(json.dumps(command) + "\\n")
    assert command[command.index("--python") + 1] == str(prefix / "bin" / "python")
    assert not any(key.startswith(("UV_", "PIP_", "FLOWTOOL_")) for key in kwargs["env"])
    assert os.fstat(kwargs["pass_fds"][0]).st_ino == prefix.stat().st_ino
    if "check" in command:
        return real_run([{shutil.which("uv")!r}, *command[1:]], **kwargs)
    if "install" in command:
        assert command[-1] == "flowtool @ git+https://github.com/b1indsight/agentflow@{SHA}"
        if fault == "install":
            return subprocess.CompletedProcess(command, 1, "", "secret install diagnostic")
        if fault == "interrupted":
            return real_run([sys.executable, "-I", "-c", {installer_wait!r}, str(prefix.parent)], **kwargs)
        provenance = json.loads((metadata / "direct_url.json").read_text())
        provenance["vcs_info"]["commit_id"] = {OLD_SHA!r} if fault == "revision" else {SHA!r}
        (metadata / "direct_url.json").write_text(json.dumps(provenance))
        (site / "app" / "__init__.py").write_text('INSTALLED_BODY = "{SHA}"\\n')
        for cache in (site / "app").rglob("__pycache__"):
            shutil.rmtree(cache)
        if fault == "body":
            (site / "app" / "__init__.py").write_text("raise RuntimeError('broken updated body')\\n")
        if fault == "cli":
            (prefix / "bin" / "flowtool").write_text(f"#!{{prefix}}/bin/python\\nraise RuntimeError('broken CLI')\\n")
        write_record(site)
    return subprocess.CompletedProcess(command, 0, "", "")
subprocess.run = run
from app.cli import app
app()
'''
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("FLOWTOOL_")
    }
    environment["PATH"] = str(prefix.parent) + os.pathsep + environment["PATH"]
    environment["UV_NO_DEPS"] = "true"
    environment["PIP_TARGET"] = str(prefix.parent / "wrong-target")
    command = [str(prefix / "bin" / "python"), "-I", "-c", code, *arguments]
    if background:
        return subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment
        )
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def set_revision(installation, revision):
    _, site = installation
    path = site / "flowtool-0.0.1.dist-info" / "direct_url.json"
    provenance = json.loads(path.read_text())
    provenance["vcs_info"]["commit_id"] = revision
    path.write_text(json.dumps(provenance))
    (site / "app" / "__init__.py").write_text(f'INSTALLED_BODY = "{revision}"\n')
    write_record(site)


@pytest.mark.parametrize("read_only", [False, True])
def test_worker_without_update_still_starts_runtime(tmp_path, monkeypatch, read_only):
    from app.worker import config

    if read_only:
        tmp_path.chmod(0o555)
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setitem(vars(config), "settings", Mock())
    runtime = Mock(run=AsyncMock())
    monkeypatch.setattr("app.worker.runtime.WorkerRuntime", Mock(return_value=runtime))
    result = CliRunner().invoke(cli.app, ["worker"])
    assert result.exit_code == 0, result.output
    runtime.run.assert_awaited_once()


def test_worker_help_advertises_update_option():
    result = CliRunner().invoke(cli.app, ["worker", "--help"])
    assert result.exit_code == 0, result.output
    assert "--update" in result.output


@pytest.mark.parametrize(
    "arguments", [("worker", "update", "--sha", SHA), ("worker", "--sha", SHA)]
)
def test_obsolete_or_invalid_update_arguments_are_rejected(arguments, monkeypatch):
    monkeypatch.setattr(
        "app.releases.latest_release", lambda: pytest.fail("invalid CLI must not query GitHub")
    )
    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 2, result.output


def test_update_replaces_body_in_place_and_exits_without_worker_credentials(installation):
    prefix, site = installation
    root = prefix.parent / "worker-root"
    root.mkdir()
    (root / "journal.json").write_text("retain journal, identity and authentication")
    result = invoke_installed(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "updated" and payload["sha"] == SHA
    assert payload["previous_sha"] == OLD_SHA
    assert payload["package_path"] == str(site / "app")
    commands = [
        json.loads(line) for line in (prefix.parent / "commands.jsonl").read_text().splitlines()
    ]
    assert len(commands) == 2 and "--reinstall-package" in commands[0] and "check" in commands[1]
    loaded = subprocess.run(
        [str(prefix / "bin" / "python"), "-I", "-c", "import app; print(app.INSTALLED_BODY)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert loaded.stdout.strip() == SHA
    assert (root / "journal.json").read_text() == "retain journal, identity and authentication"
    assert sorted(path.name for path in prefix.parent.iterdir()) == [
        "commands.jsonl",
        "installed",
        "uv",
        "worker-root",
    ]


def test_current_installation_is_verified_without_reinstalling(installation):
    prefix, _ = installation
    set_revision(installation, SHA)
    result = invoke_installed(installation)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["state"] == "already-current"
    commands = [
        json.loads(line) for line in (prefix.parent / "commands.jsonl").read_text().splitlines()
    ]
    assert len(commands) == 1 and "check" in commands[0]


@pytest.mark.parametrize(
    ("fault", "phase"),
    [
        ("publication", "publication"),
        ("downgrade", "ancestry"),
        ("install", "install"),
        ("revision", "fresh-verification"),
        ("body", "fresh-verification"),
        ("cli", "cli-verification"),
        ("dependencies", "dependencies"),
    ],
)
def test_update_failure_never_reports_success(installation, fault, phase):
    result = invoke_installed(installation, fault=fault)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"worker update stopped: {phase}:" in output and f"installed={OLD_SHA}" in output
    assert '"state":' not in output and "secret" not in output


def test_current_but_broken_dependencies_are_not_reported_current(installation):
    set_revision(installation, SHA)
    result = invoke_installed(installation, fault="dependencies")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "dependencies" in result.stderr and "already-current" not in result.stdout


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "repository",
        "sha",
        "installer",
        "permissions",
        "modified",
        "import-path",
        "entrypoint",
        "unrecorded",
        "unhashed-provenance",
        "interpreter",
    ],
)
def test_unknown_provenance_or_unsupported_ownership_fails_before_install(installation, fault):
    prefix, site = installation
    metadata = site / "flowtool-0.0.1.dist-info"
    path = metadata / "direct_url.json"
    provenance = json.loads(path.read_text())
    if fault == "missing":
        path.unlink()
    elif fault == "repository":
        provenance["url"] = "https://example.test/other/repository"
        path.write_text(json.dumps(provenance))
    elif fault == "sha":
        provenance["vcs_info"]["commit_id"] = "main"
        path.write_text(json.dumps(provenance))
    elif fault == "installer":
        (metadata / "INSTALLER").write_text("pip\n")
    elif fault == "permissions":
        prefix.chmod(0o777)
    elif fault == "modified":
        with (site / "app" / "worker" / "cli.py").open("a") as handle:
            handle.write("\n# locally modified\n")
    elif fault == "entrypoint":
        (prefix / "bin" / "flowtool").write_text(
            "#!/other/python\nfrom app.cli import app\napp()\n"
        )
    elif fault == "unrecorded":
        (site / "app" / "unexpected.py").touch()
    if fault not in ("modified", "unrecorded"):
        write_record(site)
    if fault == "unhashed-provenance":
        record = metadata / "RECORD"
        record.write_text(
            "\n".join(
                line for line in record.read_text().splitlines() if "direct_url.json" not in line
            )
            + "\n"
        )
    result = invoke_installed(installation, fault=fault)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "worker update stopped: provenance:" in result.stderr
    assert not (prefix.parent / "commands.jsonl").exists()


def start_holder(installation, *, updating=False, name="worker-a"):
    prefix, _ = installation
    ready = prefix.parent / f"{name}.ready"
    stop = prefix.parent / f"{name}.stop"
    code = f"""
import asyncio, time
from pathlib import Path
from app.worker import self_update
ready, stop = Path({str(ready)!r}), Path({str(stop)!r})
def wait():
    ready.touch()
    deadline = time.monotonic() + 20
    while not stop.exists() and time.monotonic() < deadline:
        time.sleep(.02)
if {updating!r}:
    with self_update.installation_lock(updating=True):
        wait()
else:
    from app.worker import config, runtime
    config.settings = object()
    class Runtime:
        def __init__(self, settings): pass
        async def run(self):
            lock = runtime.WorkerRootLock(Path({str(prefix.parent / name)!r}), {name!r})
            lock.acquire()
            try: wait()
            finally: lock.release()
    runtime.WorkerRuntime = Runtime
    from app.cli import app
    app()
"""
    process = subprocess.Popen(
        [str(prefix / "bin" / "python"), "-I", "-c", code, "worker"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready.exists():
        process.terminate()
        pytest.fail(str(process.communicate(timeout=5)))
    return process, stop


def test_running_workers_with_distinct_roots_exclude_update(installation):
    holders = []
    try:
        holders.append(start_holder(installation, name="worker-a"))
        holders.append(start_holder(installation, name="worker-b"))
        result = invoke_installed(installation)
        assert result.returncode == 1 and "installation" in result.stderr
        assert not (installation[0].parent / "commands.jsonl").exists()
    finally:
        for process, stop in holders:
            stop.touch()
            assert process.communicate(timeout=10) == ("", "")
            assert process.returncode == 0
    result = invoke_installed(installation)
    assert result.returncode == 0, result.stdout + result.stderr


def test_exclusive_update_lock_blocks_startup_but_not_help_or_other_installations(
    installation, tmp_path, monkeypatch
):
    process, stop = start_holder(installation, updating=True)
    try:
        result = invoke_installed(installation, arguments=("worker",))
        assert result.returncode == 1 and "installation" in result.stderr
        result = invoke_installed(installation)
        assert result.returncode == 1 and "installation" in result.stderr
        help_result = invoke_installed(installation, arguments=("worker", "--help"))
        assert help_result.returncode == 0, help_result.stdout + help_result.stderr
        # The same CLI can start against another installation while this one is updating.
        other = tmp_path / "other-installation"
        other.mkdir()
        from app.worker import config

        monkeypatch.setattr(sys, "prefix", str(other))
        monkeypatch.setitem(vars(config), "settings", Mock())
        runtime = Mock(run=AsyncMock())
        monkeypatch.setattr("app.worker.runtime.WorkerRuntime", Mock(return_value=runtime))
        result = CliRunner().invoke(cli.app, ["worker"])
        assert result.exit_code == 0, result.output
        runtime.run.assert_awaited_once()
    finally:
        stop.touch()
        process.communicate(timeout=10)
        assert process.returncode == 0


def test_installer_retains_exclusion_after_updater_is_interrupted(installation):
    # An interrupted updater must not release exclusion while uv is still replacing files.
    prefix, _ = installation
    process = invoke_installed(installation, fault="interrupted", background=True)
    ready = prefix.parent / "installer.ready"
    stop = prefix.parent / "installer.stop"
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), process.communicate(timeout=5)
        process.terminate()
        process.communicate(timeout=5)
        result = invoke_installed(installation, arguments=("worker",))
        assert (
            result.returncode == 1
            and "worker startup stopped: installation is in use" in result.stderr
        )
    finally:
        stop.touch()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
    # Confirm the child exits and releases its inherited lock before deleting the fixture.
    deadline = time.monotonic() + 5
    while True:
        result = invoke_installed(installation)
        if result.returncode == 0:
            break
        assert "installation is in use" in result.stderr and time.monotonic() < deadline, (
            result.stderr
        )
        time.sleep(0.02)
