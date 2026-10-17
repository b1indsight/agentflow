"""Build the browser application before uv build/sync; Node is not needed to serve it.

Run `python scripts/build_console.py` from a source checkout with Node 24 installed.
The generated package directory is intentionally not version controlled.
"""

import shutil
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    web = root / "app/controler/web"
    subprocess.run(["npm", "ci"], cwd=web, check=True)
    subprocess.run(["npm", "run", "build"], cwd=web, check=True)
    destination = root / "app/controler/console_static"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(web / "dist", destination)


if __name__ == "__main__":
    main()
