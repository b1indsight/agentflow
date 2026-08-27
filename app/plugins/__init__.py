"""插件层（pluggy）。见 docs/architecture/roadmap.md §9-7。

core 在生命周期端口触发 hook，插件订阅。示例：合并后总结插件。
"""

from __future__ import annotations

import pluggy

from app.plugins import builtin, hookspecs

PROJECT = "flowtool"


def get_plugin_manager() -> pluggy.PluginManager:
    pm = pluggy.PluginManager(PROJECT)
    pm.add_hookspecs(hookspecs.FlowHooks)   # specs 定义在 FlowHooks 类上，须传类而非模块
    pm.register(builtin.post_merge_summary)
    # 外部插件经 entry-point "flowtool" 加载（后于 builtin，覆盖优先）
    pm.load_setuptools_entrypoints(PROJECT)
    return pm
