"""Sandbox 端口的 adapter。

见 docs/architecture/components.md §4.6、roadmap.md §9-5。

沙盒 = 可写根 + 网络出口，只认路径不认 git/jj。这个 workspace-write adapter 是早期接缝；M1
CodeAgent 为了操作完整 Git metadata，改用显式 trusted-host full access，不再消费它。

后续 adapter（容器 / 微VM，§9-5，M5）实现同一 `policy()`，但真正**接管**文件系统与
网络策略——那时它守住整个工作区（含 `.git`/`.jj`），git `.git` 只读那道坎（原 V4）自然消失。

TODO(verify) V3：push 需要放开网络（network=True）——codex 里对应的开关键 + 尽量限到 github.com。
"""

from __future__ import annotations

from pathlib import Path

from app.models import SandboxPolicy


class WorkspaceWriteSandbox:
    """Legacy ports.Sandbox adapter retained for future non-VCS execution paths."""

    def policy(self, ws: Path, *, network: bool) -> SandboxPolicy:
        return SandboxPolicy(writable_roots=(ws,), network=network)
