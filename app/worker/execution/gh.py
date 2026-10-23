"""Per-invocation bot gh wrapper and configuration; no persistent login or token files."""

import os
import shlex
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.domain.models import GitHubCapability
from app.worker.execution.vcs import VcsVerificationError
from app.worker.execution.workspaces import validate_repository


def require_gh(capability: GitHubCapability, repository: str, path: str | None = None) -> str:
    """Validate the capability and resolve the real executable before changing PATH."""
    validate_repository(repository)
    if (capability.repository != repository or not capability.token.get_secret_value().strip()
            or capability.expires_at.tzinfo is None
            or capability.expires_at <= datetime.now(UTC)):
        raise VcsVerificationError("capability", "missing, mismatched or expired GitHub capability")
    executable = shutil.which("gh", path=path)
    if executable is None:
        raise VcsVerificationError("bootstrap", "required executable is unavailable: gh")
    return str(Path(executable).absolute())


def github_environment(
    capability: GitHubCapability, directory: Path, source: Mapping[str, str],
) -> dict[str, str]:
    """Create the wrapper/config in a caller-owned private directory and return its environment."""
    real_gh = require_gh(capability, capability.repository, source.get("PATH", os.defpath))
    binaries, config = directory / "bin", directory / "config"
    binaries.mkdir(mode=0o700)
    config.mkdir(mode=0o700)
    wrapper = binaries / "gh"
    wrapper.write_text(
        '#!/bin/sh\n'
        f'if [ "$(/bin/date +%s)" -ge {int(capability.expires_at.timestamp())} ]; then\n'
        '  printf "%s\\n" "GitHub capability expired; request a new assignment" >&2\n'
        '  exit 1\n'
        'fi\n'
        f'exec {shlex.quote(real_gh)} "$@"\n'
    )
    wrapper.chmod(0o700)
    return {
        "PATH": f"{binaries}{os.pathsep}{source.get('PATH', os.defpath)}",
        "GH_TOKEN": capability.token.get_secret_value(),
        "GH_CONFIG_DIR": str(config),
        "GH_HOST": "github.com",
        "GH_REPO": capability.repository,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
    }
