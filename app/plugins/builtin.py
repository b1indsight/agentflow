"""示例插件：合并后总结（post-merge-summary）。ARCHITECTURE §9-7。

归纳上一段对话里的代码风格偏好、更新仓库的 skill 文件。
M0：桩 + TODO(verify)。
"""

from __future__ import annotations

from loguru import logger

from app.models import Flow
from app.plugins.hookspecs import hookimpl


class PostMergeSummary:
    @hookimpl
    def on_merged(self, flow: Flow) -> None:
        # TODO(verify)：读会话 + diff → 提炼风格偏好 → 提 PR 更新 repo 的 skill 文件。
        logger.info("post-merge-summary: flow {} merged (summary TODO)", flow.id)


post_merge_summary = PostMergeSummary()
