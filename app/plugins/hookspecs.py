"""生命周期 hook 定义（pluggy hookspecs）。见 docs/architecture/roadmap.md §9-7。"""

from __future__ import annotations

import pluggy

from app.domain.models import Flow

hookspec = pluggy.HookspecMarker("flowtool")
hookimpl = pluggy.HookimplMarker("flowtool")


class FlowHooks:
    @hookspec
    def on_flow_start(self, flow: Flow) -> None:
        """flow 进入 CODING 时。"""

    @hookspec
    def on_pr_opened(self, flow: Flow, pr_number: int) -> None:
        """PR 开出后。"""

    @hookspec
    def on_review(self, flow: Flow) -> None:
        """收到一轮 review 反馈后。"""

    @hookspec
    def on_merged(self, flow: Flow) -> None:
        """PR 被人工合并后。"""

    @hookspec
    def on_archived(self, flow: Flow) -> None:
        """flow 归档后。"""
