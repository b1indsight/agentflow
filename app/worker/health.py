"""Local advisory probes; these never prove execution ownership or authorize release."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


class HealthFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, connected: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'pid': os.getpid(), 'connected': connected, 'updated': time.time()}))
        temporary.replace(self.path)


def healthy(path: Path, *, live: bool = False) -> bool:
    try:
        record = json.loads(path.read_text())
        if (type(record['pid']) is not int or record['pid'] <= 0
                or not 0 <= time.time() - record['updated'] <= 20):
            return False
        os.kill(record['pid'], 0)
        return live or record['connected'] is True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if healthy(args.path, live=args.live) else 1)


if __name__ == '__main__':
    main()
