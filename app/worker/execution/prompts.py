"""Context Builder：首轮 / 修订 prompt 构建。

见 docs/architecture/components.md §4.9、review-loop.md §B.4。

最高杠杆的组件——工具效果上限压在这里。写实（纯字符串组装）。
TODO(verify) V8：注入的 VCS 指引（尤其 jj）对 codex 的实际效果。
"""

from __future__ import annotations

from app.contracts.protocol import ReviewFeedback, RevisionTask

# 硬约束：codex 只碰本地 VCS，不碰 GitHub API（平面边界 §3）
_HARD_CONSTRAINTS = (
    "Rules:\n"
    "- Edit code to satisfy the task, then commit locally and push using this repo's VCS "
    "(see VCS guidance below).\n"
    "- After the push, call the `flow.open_pr` environment tool once with the proposed PR title "
    "and body. Do NOT use GitHub API/CLI to open a PR or post comments.\n"
    "- Run the repo's tests / lint if present and keep them green.\n"
)

# 修订轮要 codex 回的结构化产出（--output-schema 对应 models.RevisionCodingOutput）
_OUTPUT_INSTRUCTION = (
    "When done, output a JSON object with keys: summary (nonempty string), "
    "vcs_backend_used (git or jj), "
    "addressed (list of {item_id, version, comment_id, note}), "
    "deferred (list of {item_id, version, comment_id, reason}), "
    "needs_clarification (list of string)."
)

_INITIAL_OUTPUT_INSTRUCTION = (
    "When done, output a JSON object with `summary` and `vcs_backend_used`. "
    "Set `vcs_backend_used` to `git` or `jj` according to the backend you actually used."
)


def build_initial_prompt(
    issue_title: str, issue_body: str, repo_conventions: str, vcs_guidance: str
) -> str:
    parts = [
        f"# Task (from GitHub issue)\n## {issue_title}\n\n{issue_body}\n",
        f"# Repo conventions\n{repo_conventions or '(none provided)'}\n",
        f"# VCS guidance\n{vcs_guidance}\n",
        _HARD_CONSTRAINTS,
        _INITIAL_OUTPUT_INSTRUCTION,
    ]
    return "\n".join(parts)


def _format_item(item: ReviewFeedback) -> str:
    if item.kind.value == "inline" and item.diff_hunk:
        # 按内容定位，避开行号漂移（docs/architecture/review-loop.md §B.4）
        return (
            f"- [item_id={item.item_id}, version={item.version}, comment_id={item.github_id}] "
            f"file `{item.path}` "
            f"(at commit {item.original_commit_id}):\n"
            f"```diff\n{item.diff_hunk}\n```\n"
            f"  {item.author} says: {item.body}"
        )
    return (f"- [item_id={item.item_id}, version={item.version}, comment_id={item.github_id}] "
            f"{item.author} says: {item.body}")


def build_revision_prompt(task: RevisionTask, repo_conventions: str) -> str:
    """Supply enough frozen context to resume or rebuild a session in the original repo."""
    feedback = "\n".join(_format_item(i) for i in task.feedback) or "(no inline items)"
    parts = [
        f"# Original task\n{task.issue_title}\n{task.issue_body}\n",
        f"# Repo conventions\n{repo_conventions}\n",
        (f"# Delivery\n{task.repo} PR #{task.pr_number}; branch {task.branch}; "
        f"base {task.base_branch}; expected head {task.expected_head_sha}\n"),
        f"# Previous summary\n{task.previous_summary}\n",
        f"# Unresolved questions\n{chr(10).join(task.open_points)}\n",
        f"# Revision instruction (synthesized from review)\n{task.instruction}\n",
        f"# Review feedback\n{feedback}\n",
        ("Rules: Use the existing repo, branch and PR. Modify, test, commit and ordinary push "
        "to origin. Use the repo's Git or jj backend and leave a clean tree; for jj leave an "
        "empty working copy at or directly after the pushed bookmark. Do not force push, "
        "reset, open another PR, use GitHub API/CLI, post comments or request review. "
        "If the existing code already satisfies the feedback, explain why and leave the SHA "
        "unchanged. Report exact item/version/comment identities. Preserve deferred work "
        "and ask concrete clarification questions. Prior conversation may be unavailable."),
        _OUTPUT_INSTRUCTION,
    ]
    if task.context is not None:
        parts.append(f"# Frozen PR context\n{task.context.model_dump_json()}")
    return "\n".join(parts)
