"""CodeAgent 适配器：codex CLI。ARCHITECTURE §4.4 / §4.6。

已实测的调用形态（写实）：
  起始：codex exec <PROMPT> --json -C <ws> -s workspace-write --skip-git-repo-check
        -o <last> </dev/null
        → JSONL 里 {"type":"thread.started","thread_id":"<UUID>"} 取 session id
  续接：codex exec resume <sid> <PROMPT> --json --skip-git-repo-check -o <last> </dev/null
        （resume 不接受 -s/-C，从会话继承 cwd + sandbox）

沙盒不再写死：起始时向注入的 `ports.Sandbox` 要一份 SandboxPolicy（可写根 + 网络），
再翻成 codex 参数。MVP = WorkspaceWriteSandbox → `-s workspace-write`（+ push 需要的网络）。

TODO(verify) V5：改码+commit+push 在一次 run 内端到端（jj commit 已验，push 未验）。
TODO(verify) V6：多轮 resume 的 context/token 增长。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from loguru import logger

from app.config import get_settings
from app.models import AgentRun, CodexResult, SandboxPolicy
from app.ports import Sandbox
from app.sandbox import WorkspaceWriteSandbox

CODEX_BIN = "codex"


def _sandbox_args(policy: SandboxPolicy) -> list[str]:
    """把 SandboxPolicy 翻成 codex CLI 参数（workspace-write 家族）。"""
    args = ["-s", "workspace-write"]
    # workspace-write 默认把工作区设为可写、断网。额外可写根 / 放网络走 -c 覆盖。
    if policy.network:
        # TODO(verify) V3：放开出口的确切配置键 + 限到 github.com（push 才需要）。
        args += ["-c", "sandbox_workspace_write.network_access=true"]
    return args


def _parse_thread_id(jsonl_text: str) -> str | None:
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "thread.started":
            return evt.get("thread_id")
    return None


class CodexAgent:
    """实现 ports.CodeAgent。"""

    def __init__(self, sandbox: Sandbox | None = None):
        self.sandbox: Sandbox = sandbox or WorkspaceWriteSandbox()
        self.timeout = get_settings().codex_run_timeout_seconds

    async def start(self, task: str, cwd: Path, *, output_schema: Path | None = None) -> AgentRun:
        # 起始 run 要 commit + push → 需放网络（沙盒轴决定，§4.6）。
        policy = self.sandbox.policy(cwd, network=True)
        args = [
            CODEX_BIN, "exec", task,
            "--json", "-C", str(cwd),
            *_sandbox_args(policy),
            "--skip-git-repo-check",
        ]
        return await self._run(args, output_schema, parse_thread=True)

    async def resume(
        self, session_id: str, prompt: str, *, output_schema: Path | None = None
    ) -> AgentRun:
        # resume 不接受 -s / -C：cwd 与 sandbox 从会话继承
        args = [
            CODEX_BIN, "exec", "resume", session_id, prompt,
            "--json", "--skip-git-repo-check",
        ]
        return await self._run(args, output_schema, parse_thread=False, session_id=session_id)

    async def _run(
        self,
        args: list[str],
        output_schema: Path | None,
        *,
        parse_thread: bool,
        session_id: str | None = None,
    ) -> AgentRun:
        last_file = Path(tempfile.mktemp(prefix="codex-last-", suffix=".txt"))
        args += ["-o", str(last_file)]
        if output_schema is not None:
            args += ["--output-schema", str(output_schema)]

        logger.info("codex run: {}", " ".join(a if len(a) < 60 else a[:57] + "..." for a in args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,   # 必须：否则 codex 会挂等 stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error("codex run timed out after {}s", self.timeout)
            return AgentRun(session_id=session_id, last_message="", exit_code=124)

        jsonl = stdout.decode(errors="replace")
        sid = _parse_thread_id(jsonl) if parse_thread else session_id
        last_message = last_file.read_text() if last_file.exists() else ""

        output: CodexResult | None = None
        if output_schema is not None and last_message.strip():
            # 结构化产出：codex 把最终 JSON 写进 -o 文件（符合 --output-schema）
            try:
                output = CodexResult.model_validate_json(last_message)
            except Exception as e:  # noqa: BLE001
                logger.warning("failed to parse CodexResult: {}", e)  # TODO(verify) 产出落点

        if last_file.exists():
            last_file.unlink()
        if proc.returncode != 0:
            logger.error("codex exit={} stderr={}", proc.returncode, stderr.decode()[:500])

        return AgentRun(
            session_id=sid, last_message=last_message, exit_code=proc.returncode or 0, output=output
        )
