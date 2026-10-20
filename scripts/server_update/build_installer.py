"""Bundle the webhook updater for transfer to the host, without installing or deploying.

Example: python scripts/server_update/build_installer.py --output /tmp/install-updater.py
"""

import argparse
import base64
import gzip
import hashlib
import io
import tarfile
import textwrap
from pathlib import Path


def build() -> str:
    """Produce a reproducible, checksum-pinned installer from these canonical sources."""
    root = Path(__file__).parent
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("update-agentflow-server.py", "agentflow-maintenance.py", "column-check.py"):
            data = (root / name).read_bytes()
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(data), 0o600
            archive.addfile(item, io.BytesIO(data))
    payload = gzip.compress(buffer.getvalue(), mtime=0)
    output = (
        (root / "install.py.in")
        .read_text()
        .replace("__BUNDLE_SHA256__", hashlib.sha256(payload).hexdigest())
        .replace(
            '"__BUNDLE_BASE64__"',
            "(\n"
            + "\n".join(
                f'    "{line}"'
                for line in textwrap.wrap(
                    base64.b64encode(payload).decode(),
                    96,
                )
            )
            + "\n)",
        )
    )
    compile(output, "install-agentflow-updater.py", "exec")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(build())
    args.output.chmod(0o600)
    print(f"{hashlib.sha256(args.output.read_bytes()).hexdigest()}  {args.output}")
