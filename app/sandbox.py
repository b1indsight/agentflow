"""Sandbox 端口的 adapter。ARCHITECTURE §4.6 / §9-5。

沙盒 = 可写根 + 网络出口，三正交轴之一，只认路径不认 git/jj。MVP adapter 就是
codex 原生的 `workspace-write`：把「工作区可写、默认断网」表达成一份 SandboxPolicy，
由 code agent（codex_driver）翻成 CLI 参数。

后续 adapter（容器 / 微VM，§9-5，M5）实现同一 `policy()`，但真正**接管**文件系统与
网络策略——那时它守住整个工作区（含 `.git`/`.jj`），git `.git` 只读那道坎（原 V4）自然消失。

TODO(verify) V3：push 需要放开网络（network=True）——codex 里对应的开关键 + 尽量限到 github.com。
"""

from __future__ import annotations

from pathlib import Path

from app.models import SandboxPolicy


class WorkspaceWriteSandbox:
    """实现 ports.Sandbox。codex 原生 workspace-write：工作区可写，默认不放网。"""

    def policy(self, ws: Path, *, network: bool) -> SandboxPolicy:
        return SandboxPolicy(writable_roots=(ws,), network=network)
