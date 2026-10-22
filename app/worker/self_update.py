"""Update a user-owned, uv-installed Git package in its existing virtual environment.

PEP 610 provenance and the installed RECORD identify the package; no cached revision
marker is authoritative. See docs/deployment/worker-update.md for the supported contract.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import httpx

from app import releases

_SOURCE = "https://github.com/b1indsight/agentflow"


class UpdateError(RuntimeError):
    """The installation cannot safely be used or updated; contains no subprocess output."""


@dataclass(frozen=True)
class InstalledPackage:
    """Installed Git revision and the import directory validated against its RECORD."""

    sha: str
    package_path: Path


@contextmanager
def installation_lock(*, updating: bool) -> Iterator[int]:
    """Share the prefix directory lock for Worker lifetimes; exclude it for updates.

    The directory inode survives package replacement. Locking it needs no writable lock
    file, including for ordinary Workers in read-only, container-owned installations.
    WorkerRootLock still protects each individual root inside this shared lock.
    """
    try:
        descriptor = os.open(sys.prefix, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise UpdateError("cannot open installation for locking") from None
    try:
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_EX if updating else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("installation is in use by another Worker or update") from None
        except OSError:
            raise UpdateError("installation does not support directory flock") from None
        yield descriptor
    finally:
        os.close(descriptor)


def _owned(path: Path, prefix: Path) -> None:
    """Reject paths outside the environment, symlinks and unsupported write ownership."""
    if not path.is_relative_to(prefix):
        raise UpdateError("package files are outside the current virtual environment")
    for item in (path, *path.parents):
        details = item.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_mode & 0o022
            or not details.st_mode & stat.S_IWUSR
        ):
            raise UpdateError(
                "installation must be writable, user-owned and not group/world-writable"
            )
        if item == prefix:
            break


def installed_package() -> InstalledPackage:
    """Read verified local package provenance, ownership, import locations and RECORD.

    This intentionally supports one installation contract, with no source-update path or
    installer fallback. RECORD detects local modifications, not malicious metadata from
    the installation owner; the existing environment and its metadata must be trusted.
    """
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or not Path(sys.executable).is_relative_to(prefix):
        raise UpdateError("update requires the existing virtual environment's Python")
    _owned(prefix, prefix)
    site = Path(sysconfig.get_path("purelib"))
    _owned(site, prefix)
    distribution = metadata.distribution("flowtool")
    if Path(str(distribution.locate_file(""))).resolve() != site.resolve():
        raise UpdateError("flowtool metadata is not in this interpreter's site-packages")
    if (distribution.read_text("INSTALLER") or "").strip() != "uv":
        raise UpdateError("only uv-owned installations are supported")
    provenance = json.loads(distribution.read_text("direct_url.json") or "null")
    if not isinstance(provenance, dict):
        raise UpdateError("missing PEP 610 Git provenance")
    vcs = provenance.get("vcs_info")
    if (
        set(provenance) != {"url", "vcs_info"}
        or provenance.get("url") not in (_SOURCE, _SOURCE + ".git")
        or not isinstance(vcs, dict)
        or vcs.get("vcs") != "git"
        or not isinstance(vcs.get("commit_id"), str)
        or not re.fullmatch("[0-9a-f]{40}", vcs["commit_id"])
    ):
        raise UpdateError(
            "expected fixed-repository Git provenance with an immutable full commit_id"
        )
    files = distribution.files
    if not files:
        raise UpdateError("missing installed RECORD")
    recorded: set[Path] = set()
    for entry in files:
        path = Path(os.path.abspath(str(distribution.locate_file(entry))))
        _owned(path, prefix)
        recorded.add(path)
        if (
            entry.name == "RECORD" and entry.parent.name.endswith(".dist-info")
        ) or path.suffix == ".pyc":
            continue
        if entry.hash is None or entry.hash.mode != "sha256":
            raise UpdateError("installed RECORD must contain SHA-256 hashes")
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest())
            .decode()
            .rstrip("=")
        )
        if digest != entry.hash.value:
            raise UpdateError("installed package was modified or is incomplete; refusing overwrite")
    metadata_paths = [
        path.parent
        for path in recorded
        if path.name == "METADATA" and path.parent.name.endswith(".dist-info")
    ]
    if len(metadata_paths) != 1 or not {
        metadata_paths[0] / name
        for name in ("METADATA", "INSTALLER", "RECORD", "direct_url.json", "entry_points.txt")
    }.issubset(recorded):
        raise UpdateError("installed RECORD does not cover the package provenance and entry point")
    if any(
        path.is_file() and path.suffix != ".pyc" and path not in recorded
        for path in (site / "app").rglob("*")
    ):
        raise UpdateError("unrecorded package files exist; refusing overwrite")
    for name in ("app", "app.cli", "app.releases", "app.worker.cli", __name__):
        module = importlib.import_module(name)
        relative = "app/__init__.py" if name == "app" else name.replace(".", "/") + ".py"
        expected = site / relative
        if expected not in recorded or Path(module.__file__ or "").absolute() != expected:
            raise UpdateError("actual import path does not match the installed package RECORD")
    points = [
        point
        for point in distribution.entry_points
        if point.group == "console_scripts" and point.name == "flowtool"
    ]
    entrypoint = prefix / "bin" / "flowtool"
    if len(points) != 1 or points[0].value != "app.cli:app" or entrypoint not in recorded:
        raise UpdateError("unsupported flowtool console entry point")
    if entrypoint.read_text().partition("\n")[0] != f"#!{sys.executable}":
        raise UpdateError("flowtool entry point does not use this virtual environment's Python")
    return InstalledPackage(sha=vcs["commit_id"], package_path=site / "app")


def _require_forward(current: str, target: str) -> None:
    """Use repositories.Repository.compare_commits' predicate without App credentials."""
    try:
        with httpx.stream(
            "GET",
            f"https://api.github.com/repos/{releases.REPOSITORY}/compare/{current}...{target}?per_page=1",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "agentflow-updates"},
            timeout=20,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise UpdateError("Git ancestry response exceeds the size limit")
        comparison = json.loads(body)
        if (
            comparison.get("status") != "ahead"
            or comparison.get("base_commit", {}).get("sha") != current
            or comparison.get("merge_base_commit", {}).get("sha") != current
            or comparison.get("behind_by") != 0
        ):
            raise UpdateError(
                "target is not a verified forward update; refusing downgrade/divergence"
            )
    except (httpx.HTTPError, ValueError, AttributeError):
        raise UpdateError("cannot verify forward Git ancestry") from None


def _run(command: list[str], lock_descriptor: int, *, timeout: int = 60) -> str:
    # Ignore installer overrides and do not pass Worker credentials into builds/checks.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UV_", "PIP_", "FLOWTOOL_"))
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    result = subprocess.run(
        command,
        cwd=sys.prefix,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        # An installer must retain exclusion even if its calling updater is interrupted.
        pass_fds=(lock_descriptor,),
    )
    if result.returncode:
        raise UpdateError(
            f"subprocess failed (exit {result.returncode}); inspect/repair the existing installation"
        )
    return result.stdout


def update_worker() -> dict[str, str]:
    """Verify latest publication, update this package in place, verify fresh processes, exit."""
    current = target = "unknown"
    phase = "provenance"
    with installation_lock(updating=True) as lock_descriptor:
        try:
            installed = installed_package()
            current = installed.sha
            uv = shutil.which("uv")
            if uv is None:
                raise UpdateError("uv is required on PATH for the supported installation")
            phase = "publication"
            target = releases.latest_release().sha
            if current != target:
                phase = "ancestry"
                _require_forward(current, target)
                phase = "install"
                _run(
                    [
                        uv,
                        "--no-config",
                        "pip",
                        "install",
                        "--python",
                        sys.executable,
                        "--no-python-downloads",
                        "--link-mode",
                        "copy",
                        "--reinstall-package",
                        "flowtool",
                        f"flowtool @ git+{_SOURCE}@{target}",
                    ],
                    lock_descriptor,
                    timeout=600,
                )
            phase = "fresh-verification"
            loaded = json.loads(
                _run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        (
                            "import json; from app.worker.self_update import installed_package; "
                            "p = installed_package(); "
                            "print(json.dumps({'sha': p.sha, 'package_path': str(p.package_path)}))"
                        ),
                    ],
                    lock_descriptor,
                )
            )
            if loaded != {"sha": target, "package_path": str(installed.package_path)}:
                raise UpdateError(
                    "fresh interpreter did not load the target revision at the original path"
                )
            phase = "cli-verification"
            help_text = _run(
                [
                    sys.executable,
                    "-I",
                    str(Path(sys.prefix) / "bin" / "flowtool"),
                    "worker",
                    "--help",
                ],
                lock_descriptor,
            )
            if "--update" not in help_text:
                raise UpdateError("installed Worker CLI does not expose --update")
            phase = "dependencies"
            _run(
                [
                    uv,
                    "--no-config",
                    "pip",
                    "check",
                    "--python",
                    sys.executable,
                    "--no-python-downloads",
                ],
                lock_descriptor,
            )
            return {
                "state": "already-current" if current == target else "updated",
                "previous_sha": current,
                "sha": target,
                "package_path": str(installed.package_path),
                "python": sys.executable,
            }
        except (
            UpdateError,
            releases.ReleaseError,
            OSError,
            ValueError,
            metadata.PackageNotFoundError,
            subprocess.SubprocessError,
        ) as error:
            # Arbitrary subprocess output may contain private index URLs or build credentials.
            reason = (
                str(error)
                if isinstance(error, (UpdateError, releases.ReleaseError))
                else type(error).__name__
            )
            raise UpdateError(f"{phase}: {reason}; installed={current} target={target}") from None
