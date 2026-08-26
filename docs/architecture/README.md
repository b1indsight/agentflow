# FlowTool 架构文档

> 状态：M0 已验收、M1+ 仍为草案 · 初始日期：2026-08-02 · M0 验收：2026-08-26。
> 本文件是 living doc，细节随讨论迭代。
> 项目暂用代号 **FlowTool**（源自触发标签 `flow`），未定名。
> 2026-08-02 修订：本地平面拆成 **workspace / sandbox / VCS 三条正交轴**——VCS 降为普通函数（不再是端口），
> Sandbox 提为一等端口（§4.6 / §9-5）。

本目录是 FlowTool 架构文档的唯一入口。当前实现状态与目标架构的语义同步将在
[项目文档完整同步计划](../plan/documentation-refresh.md)的后续变更中完成。

## 阅读导航

- [总览](overview.md)：定位、运行形态、平面边界与组件总览；
- [组件职责](components.md)：端口、适配器、状态机与持久层；
- [端到端流程](workflows.md)：目标闭环时序；
- [关键决策、风险与短板](decisions.md)：已定方案和开放问题；
- [路线图](roadmap.md)：M0–M5 里程碑与扩展预留；
- [GitHub App 接入](github-app.md)：认证、权限、Webhook 和密钥；
- [Review 修订循环](review-loop.md)：triage、revise 与并发语义；
- [架构可视化](diagram.html)：手工维护的交互式组件图。

