"""Flow 状态机 + 转移动作。

见 docs/architecture/components.md §4.3、review-loop.md。

编排大脑：把 store、CodeAgent、Judge、GitHubClient 串起来；工作区隔离 + VCS 指引
经 app.vcs 的普通函数（§4.6 三轴，VCS 已不是端口）。
状态转移处触发生命周期 hook（§9-7）：on_flow_start / on_pr_opened / on_review / on_merged / on_archived。
控制流写实；集成点调各适配器（部分为桩，标 TODO(verify)）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from loguru import logger

from app.config import get_settings
from app.context_builder import build_initial_prompt, build_revision_prompt
from app.models import (
    Attempt,
    CodexResult,
    Flow,
    FlowState,
    Job,
    JobKind,
    TriageVerdict,
    Verdict,
)
from app.plugins import get_plugin_manager
from app.store import Store
from app.vcs import agent_guidance, cleanup_workspace, make_workspace, workspace_path


class FlowEngine:
    def __init__(self, store: Store, code_agent, judge, github, plugins=None) -> None:
        self.store = store
        self.code_agent = code_agent
        self.judge = judge
        self.github = github
        self.plugins = plugins or get_plugin_manager()   # 生命周期 hook（§9-7）
        self.settings = get_settings()

    def _fire(self, hook: str, **kw) -> None:
        """触发一个生命周期 hook。插件异常不许掀翻 core——吞掉并记日志。"""
        try:
            getattr(self.plugins.hook, hook)(**kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("plugin hook {} failed: {}", hook, e)

    # --------------------------------------------------------------- 触发 / 取消

    def on_trigger(self, repo: str, issue_number: int, installation_id: int | None) -> Flow | None:
        if self.store.get_active_flow(repo, issue_number):
            logger.info("flow already active for {}#{}", repo, issue_number)
            return None
        flow = self.store.create_flow(repo, issue_number, installation_id)
        self.store.enqueue_job(flow.id, JobKind.INITIAL, payload={"issue_number": issue_number})
        logger.info("flow {} created (PENDING) for {}#{}", flow.id, repo, issue_number)
        return flow

    def cancel(self, flow: Flow) -> None:
        # TODO(verify): 若有在跑的 codex 进程，这里要 kill（§4.3 边界）
        flow.state = FlowState.CANCELLED
        self.store.update_flow(flow)
        logger.info("flow {} cancelled", flow.id)

    async def _fail(self, flow: Flow, number: int | None, reason: str) -> None:
        """codex 失败/超时或失控闸：转 FAILED，尽量在 issue/PR 上留言，不再开 PR / re-review。"""
        logger.warning("flow {} failed: {}", flow.id, reason)
        flow.state = FlowState.FAILED
        self.store.update_flow(flow)
        if number is not None:
            try:
                await self.github.comment(flow.installation_id, flow.repo, number, f"⚠️ {reason}")
            except Exception as e:  # noqa: BLE001
                logger.warning("failed to post failure comment on {}: {}", number, e)

    # ------------------------------------------------------------------- job 执行

    async def run_job(self, job: Job) -> None:
        flow = self.store.get_flow(job.flow_id)
        if flow is None or flow.state in (FlowState.CANCELLED, FlowState.ARCHIVED):
            return
        if job.kind == JobKind.INITIAL:
            await self._run_initial(flow)
        elif job.kind == JobKind.TRIAGE:
            await self.on_feedback_settled(flow)
        elif job.kind == JobKind.REVISION:
            await self._run_revision(flow, job)
        elif job.kind == JobKind.ANSWER:
            await self._run_answer(flow, job)
        elif job.kind == JobKind.ARCHIVE:
            await self._archive(flow)

    async def _run_initial(self, flow: Flow) -> None:
        flow.state = FlowState.CODING
        self.store.update_flow(flow)
        self._fire("on_flow_start", flow=flow)

        repo_path = self._repo_path(flow.repo)
        branch = f"flow/issue-{flow.issue_number}"

        # 建一个 attempt（候选解法）并设为当前（§9-2）。MVP 每 flow 一个。
        attempt = self.store.create_attempt(flow.id)
        attempt.branch = branch
        self.store.update_attempt(attempt)
        flow.current_attempt_id = attempt.id
        self.store.update_flow(flow)

        ws = await make_workspace(repo_path, branch)   # VCS-无关拷贝（§4.6 workspace 轴）
        issue = self._fetch_issue(flow)  # TODO(verify) V1 从 GitHub 取 issue 正文
        prompt = build_initial_prompt(
            issue["title"], issue["body"], self._repo_conventions(ws),
            agent_guidance(repo_path, branch),   # VCS 轴：本仓库怎么建分支 / commit / push
        )
        run = await self.code_agent.start(prompt, ws)
        attempt.codex_session_id = run.session_id
        self.store.update_attempt(attempt)
        self.store.record_codex_run(attempt.id, "initial", prompt, "", run.exit_code)
        if run.exit_code != 0:
            # 超时/报错 → 没 commit+push，别开指向空分支的 PR（§4.3 边界）。
            await self._fail(
                flow, flow.issue_number, f"编码失败（codex exit={run.exit_code}），未开 PR。"
            )
            return

        # codex 已在本地 commit + push（平面边界）。Orchestrator 据分支开 PR。
        pr_number = await self.github.open_pr(
            flow.installation_id, flow.repo, head=branch, base="main",
            title=f"{issue['title']} (closes #{flow.issue_number})",
            body=f"Closes #{flow.issue_number}\n\n{run.last_message}",
        )
        attempt.pr_number = pr_number
        self.store.update_attempt(attempt)
        flow.state = FlowState.PR_OPEN
        self.store.update_flow(flow)
        await self.github.request_reviewers(
            flow.installation_id, flow.repo, pr_number, self.settings.allowlist
        )
        await self.github.set_labels(flow.installation_id, flow.repo, pr_number, ["flow:in-review"])
        self._fire("on_pr_opened", flow=flow, pr_number=pr_number)
        logger.info("flow {} attempt {} PR #{} opened", flow.id, attempt.id, pr_number)

    async def _run_revision(self, flow: Flow, job: Job) -> None:
        attempt = self.store.get_current_attempt(flow)
        if attempt is None:
            logger.error("flow {} revision with no current attempt", flow.id)
            return
        flow.state = FlowState.REVISING
        self.store.update_flow(flow)
        attempt.revision_cycles += 1
        self.store.update_attempt(attempt)
        if attempt.revision_cycles > self.settings.max_revision_cycles:
            await self._fail(flow, attempt.pr_number, "已达最大修订轮数，暂停。")  # 失控闸（§4.4）
            return

        items = self.store.unrelayed_items(attempt.id)
        instruction = (job.payload or {}).get("instruction", "Address the review feedback.")
        guidance = agent_guidance(self._repo_path(flow.repo), attempt.branch or "")
        prompt = build_revision_prompt(instruction, items, guidance)

        schema = _write_output_schema()
        run = await self.code_agent.resume(attempt.codex_session_id, prompt, output_schema=schema)
        self.store.record_codex_run(attempt.id, "revision", prompt, "", run.exit_code)
        if run.exit_code != 0:
            # 超时/报错：本轮没更新，反馈退回未回传（不 mark_relayed）留待下轮重试（附录 B.6）。
            await self._fail(
                flow, attempt.pr_number, f"修订失败（codex exit={run.exit_code}），本轮未更新。"
            )
            return

        result = run.output or CodexResult()
        await self._apply_codex_result(flow, attempt, result)
        self.store.mark_relayed([i.id for i in items if i.id is not None])

        flow.state = FlowState.PR_OPEN
        self.store.update_flow(flow)
        await self.github.request_reviewers(
            flow.installation_id, flow.repo, attempt.pr_number, self.settings.allowlist
        )
        await self.github.set_labels(
            flow.installation_id, flow.repo, attempt.pr_number, ["flow:in-review"]
        )

    async def _apply_codex_result(self, flow: Flow, attempt: Attempt, result: CodexResult) -> None:
        iid = flow.installation_id
        for a in result.addressed:
            # 自动 resolve 已处理线程 + 回 note（附录 B.4）
            thread_id = self._thread_id_for(attempt, a.comment_id)
            if thread_id:
                await self.github.resolve_review_thread(iid, thread_id)
            self.store.mark_resolved(attempt.id, a.comment_id)
        for d in result.deferred:
            await self.github.comment(iid, flow.repo, attempt.pr_number, f"@ {d.comment_id}: {d.reason}")
        for q in result.needs_clarification:
            await self.github.comment(iid, flow.repo, attempt.pr_number, f"❓ {q}")
        if result.summary:
            await self.github.comment(
                iid, flow.repo, attempt.pr_number, f"已按 review 更新：{result.summary}"
            )

    async def _run_answer(self, flow: Flow, job: Job) -> None:
        attempt = self.store.get_current_attempt(flow)
        if attempt is None:
            return
        question = (job.payload or {}).get("question", "")
        answer = await self.judge.answer(question, self._pr_context(flow, attempt))
        await self.github.comment(flow.installation_id, flow.repo, attempt.pr_number, answer)

    async def _archive(self, flow: Flow) -> None:
        flow.state = FlowState.ARCHIVED
        self.store.update_flow(flow)
        # 清理所有 attempt 的工作区（MVP 一个；多方案时逐个清）。
        for attempt in self.store.list_attempts(flow.id):
            if attempt.branch:
                await cleanup_workspace(workspace_path(self._repo_path(flow.repo), attempt.branch))
        self._fire("on_archived", flow=flow)
        # TODO(verify): Project 卡片移 Done、贴归档小结
        logger.info("flow {} archived", flow.id)

    # ------------------------------------------------------ 反馈 → 判定（附录 B）

    async def on_feedback_settled(self, flow: Flow) -> None:
        """settle 窗口到点后调用：语义判定 → 决定 revise/answer/wait/ignore。"""
        attempt = self.store.get_current_attempt(flow)
        if attempt is None:
            return
        items = self.store.unrelayed_items(attempt.id)
        if not items:
            return
        self._fire("on_review", flow=flow)
        discussion = "\n".join(f"{i.author}: {i.body}" for i in items)
        verdict: TriageVerdict = await self.judge.triage(discussion, self._pr_context(flow, attempt))
        logger.info("flow {} triage -> {} ({})", flow.id, verdict.verdict, verdict.confidence)

        if verdict.verdict == Verdict.REVISE and verdict.confidence >= self.settings.confidence_threshold:
            await self.github.comment(
                flow.installation_id, flow.repo, attempt.pr_number, f"按此修改中：{verdict.instruction}"
            )
            self.store.enqueue_job(flow.id, JobKind.REVISION,
                                   payload={"instruction": verdict.instruction})
        elif verdict.verdict == Verdict.ANSWER:
            self.store.enqueue_job(flow.id, JobKind.ANSWER, payload={"question": discussion})
        else:
            logger.info("flow {} -> {} (no action)", flow.id, verdict.verdict)  # wait / ignore

    # ---------------------------------------------------------------- 合并 → 归档

    def on_merged(self, flow: Flow) -> None:
        flow.state = FlowState.MERGED
        self.store.update_flow(flow)
        self._fire("on_merged", flow=flow)
        self.store.enqueue_job(flow.id, JobKind.ARCHIVE)

    # ------------------------------------------------------------------ 辅助（桩）

    def _repo_path(self, repo: str) -> Path:
        return self.settings.repos_root / repo

    def _repo_conventions(self, ws: Path) -> str:
        for name in ("AGENTS.md", "CLAUDE.md"):
            p = ws / name
            if p.exists():
                return p.read_text()
        return ""

    def _fetch_issue(self, flow: Flow) -> dict:
        # TODO(verify) V1：从 GitHub 取 issue 标题/正文
        return {"title": f"issue #{flow.issue_number}", "body": ""}

    def _thread_id_for(self, attempt: Attempt, comment_id: int) -> str | None:
        # TODO(verify) V1：comment_id → GraphQL thread_id（list_review_threads 建映射）
        return None

    def _pr_context(self, flow: Flow, attempt: Attempt) -> str:
        return f"{flow.repo} PR #{attempt.pr_number} (issue #{flow.issue_number})"


def _write_output_schema() -> Path:
    """把 CodexResult 的 JSON Schema 落到临时文件，供 codex --output-schema。"""
    import json

    schema = CodexResult.model_json_schema()
    p = Path(tempfile.mktemp(prefix="codex-schema-", suffix=".json"))
    p.write_text(json.dumps(schema))
    return p
