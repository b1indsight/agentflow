"""VCS 轴：工作区隔离 + 提交指引。ARCHITECTURE §4.6。

不是端口——只是一组普通函数（旧的 VCS `Protocol` 已砍：薄包装、`head_commit`/
`rebase_onto`/`cleanup` 是死代码、只 jj 验过）。§4.6 把本地平面拆成三条正交轴：

  workspace  每 flow 一个隔离目录（**VCS-无关**的拷贝，本模块 make_workspace/cleanup_workspace）
  sandbox    可写根 + 网络出口（ports.Sandbox，只认路径，见 app/sandbox.py）
  VCS        branch / commit / push 由 code agent 自己做——本模块只出**指引文本**

平面边界（§3）：Orchestrator 定分支名（它来开 PR），agent 依 agent_guidance() 建分支 +
commit + push。工具不出现裸 git。

实测（§4.6）：workspace-write 下 jj `.jj` 可写、`jj commit` 成功；git `.git` 被沙盒挡成只读。
这是**沙盒**在漏 VCS 知识（它守 `.git` 却不知道守 `.jj`），不是 VCS 轴的问题——
未来容器 Sandbox adapter 接管 FS 策略即消除（原 V4，见 ports.Sandbox）。
TODO(verify) V8：codex 对 jj 不熟，agent_guidance() 的 jj 指引效果。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.models import VcsKind


async def _run(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


# --------------------------------------------------------------- workspace 轴（VCS-无关）


def workspace_path(repo_path: Path, branch: str) -> Path:
    return repo_path.parent / f"{repo_path.name}--{branch.replace('/', '-')}"


async def make_workspace(repo_path: Path, branch: str) -> Path:
    """把主 clone 复制成每 flow 独立工作区。**VCS-无关**——就是拷目录，不跑
    `git worktree` / `jj workspace`（那会把 workspace 绑死到具体 VCS）。

    分支名由 Orchestrator 传入（它来开 PR）；建分支 + commit + push 是 agent 的活
    （见 agent_guidance()）。TODO(verify)：大 repo 用 APFS clonefile / reflink 降成本。
    """
    ws = workspace_path(repo_path, branch)
    await _run("cp", "-a", str(repo_path), str(ws))
    return ws


async def cleanup_workspace(ws: Path) -> None:
    """删除隔离工作区（VCS-无关，就是删目录）。归档 / 取消时调。"""
    await _run("rm", "-rf", str(ws))


# ------------------------------------------------------------------- VCS 轴（指引文本）


def detect_vcs_kind(repo_path: Path) -> VcsKind:
    """探测：有 .jj → jj，否则 git。只用来挑指引文本。"""
    return VcsKind.JJ if (repo_path / ".jj").exists() else VcsKind.GIT


_GIT_GUIDANCE = (
    "This repo uses git. Create and switch to a branch named `{branch}`, commit your changes "
    "locally, then push that branch to origin. Do NOT open a pull request (the tool does that)."
)

_JJ_GUIDANCE = (
    "This repository uses Jujutsu (jj), NOT git. After editing, commit with "
    "`jj commit -m '<message>'`, set bookmark `{branch}` on that commit, then push it with "
    "`jj git push -b {branch}`. Do not run git. Do NOT open a pull request (the tool does that)."
)


def agent_guidance(repo_path: Path, branch: str) -> str:
    """注入 prompt 的本地版本控制指引（本仓库怎么建分支 / commit / push）。"""
    if detect_vcs_kind(repo_path) is VcsKind.JJ:
        return _JJ_GUIDANCE.format(branch=branch)
    return _GIT_GUIDANCE.format(branch=branch)
