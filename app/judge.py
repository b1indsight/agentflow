"""Judge 适配器：bub（小型语义判定 agent）。ARCHITECTURE §4.10、附录 B。

用法：把 PR 讨论快照喂给 bub，让它判定 revise/wait/answer/ignore 并综合出 instruction。

TODO(verify) V2：bub 的调用方式与结构化产出。
  - bub 是 hook-first runtime（pre-0.4，API 会变）。这里先用 subprocess `bub run` 兜底，
    prompt 要求它只输出一个 JSON；日后可换成库方式（自定 plugin 的 build_prompt/run_model）。
  - 模型经 BUB_MODEL / BUB_API_KEY（bub 自读环境变量）。
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger

from app.models import TriageVerdict, Verdict

_TRIAGE_PROMPT = """\
You are a triage judge on a GitHub pull request conversation involving multiple people.
Decide what the code agent should do next. Output ONLY a JSON object:
{{"verdict": "revise|wait|answer|ignore", "confidence": 0.0-1.0, "reason": "...",
  "instruction": "if revise: the concrete change to make, synthesized from the discussion",
  "answer": "if answer: the reply to post", "open_points": ["if wait: undecided points"]}}

Guidance:
- revise: the discussion reached a conclusion that requires a code change.
- wait: people are still discussing / no conclusion. Prefer this when ambiguous.
- answer: someone asks a question about the code that needs a reply, not a change.
- ignore: chatter / praise / off-topic.

## PR context
{pr_context}

## New discussion
{discussion}
"""


class BubJudge:
    """实现 ports.Judge。"""

    async def triage(self, discussion: str, pr_context: str) -> TriageVerdict:
        prompt = _TRIAGE_PROMPT.format(pr_context=pr_context, discussion=discussion)
        raw = await self._bub_run(prompt)
        try:
            data = json.loads(_extract_json(raw))
            return TriageVerdict.model_validate(data)
        except Exception as e:  # noqa: BLE001
            logger.warning("judge parse failed ({}); defaulting to wait. raw={}", e, raw[:200])
            # 保守默认：解析不了就 wait（附录 B.3）
            return TriageVerdict(verdict=Verdict.WAIT, confidence=0.0, reason="parse-failed")

    async def answer(self, question: str, pr_context: str) -> str:
        prompt = f"Answer this question about the PR concisely.\n\n{pr_context}\n\nQ: {question}"
        return (await self._bub_run(prompt)).strip()

    async def _bub_run(self, prompt: str) -> str:
        # TODO(verify) V2：确认 `bub run` 的确切用法与输出格式；必要时改库调用。
        proc = await asyncio.create_subprocess_exec(
            "bub", "run", prompt,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            logger.error("bub run failed: {}", err.decode()[:300])
        return out.decode(errors="replace")


def _extract_json(text: str) -> str:
    """从可能带前后缀的输出里抠出第一个 {...} JSON 块。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no json object found")
    return text[start : end + 1]
