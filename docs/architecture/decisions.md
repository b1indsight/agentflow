# 当前架构决策与边界

## 任务身份、交付和工作区分别归谁？

| 决策 | 调用方可依赖的行为 | 实现依据 |
|---|---|---|
| Flow 保存任务身份，Attempt 保存一次开工 | 来源是 Issue 或持久化的任务正文。失败后重试沿用当前 Attempt；归档后显式重新开工才创建新 Attempt。旧分支、PR、会话和归档证据仍属于旧 Attempt。 | [模型与状态](../../app/domain/models.py)、[重新开工的前置检查](../../app/controler/workflow/service.py)、[表约束](../../app/persistence/schema.py) |
| 初次交付和人工接手是两个合法结果 | 编码 Agent 可通过 `flow.open_pr` 登记交付意图，或通过 `flow.request_user_input` 留下待办；二者互斥。`review` 本身不能证明已有 PR。`flow.search_flows` 是按当前仓库限域的只读查询，不会完成任务或派生 Flow。 | [worker 工具](../../app/worker/transport/mcp.py)、[远程交付行为测试](../../tests/test_remote_e2e.py) |
| control 负责 PR，worker 负责代码 | Agent 选择 Git 或 jj，worker 核验指定分支、工作区和远端提交；control 独立核对 GitHub ref、终态结果与工具回执，才创建或复用 PR。外部 push 可以成为下一轮修订的基线；在途变更不能靠强推覆盖。合并由人按目标仓库的审核和 CI 决定。 | [VCS 核验](../../app/worker/execution/vcs.py)、[工具服务](../../app/controler/workflow/tools.py)、[修订契约](review-loop.md) |
| 编码固定位置，推理单独调度 | 绑定记录标识 worker、workspace 与 generation。同一 worker 的其他位置可并发执行，但不能借用仍有旧执行的位置。命名和 Judge 使用推理 Job，不迁移正在编码的会话。 | [分配与租约](../../app/persistence/assignments.py)、[推理 Job](../../app/controler/workflow/inference.py) |
| 业务归档与释放资源分开 | 当前 PR 合并可先记录成功归档；worker 尚未证明执行停止或 GitHub 操作未结清时保留绑定。此时详情可显示已归档，但不能重新开工。归档不是清理完成的回执。 | [合并处理](../../app/controler/workflow/lifecycle.py)、[控制台行为测试](../../tests/test_console_api.py) |

## 哪些数据可以重放？

**Flow 状态。** PostgreSQL 在同一事务内追加 Flow 事实并更新 `flows` 对应的账本列。`agentflow flow verify` 比较重放结果，不修表、不重建 Job，也不为缺失的历史补造起点。会话、交付、执行绑定与反馈各自有独立证据。`console_activity` 也用于展示和排队用户指令，只有其中定义为 Flow 事实的行参与重放。见[账本字段与重放规则](flow-ledger.md)。

**Job 结果。** worker 在本地 journal 保留终态，control 将接收的结果与摘要持久化后恢复应用。连接断开可以重传已知结果，但不能据此重跑一个可能已经执行的命令。未知执行、未确认停止或损坏的恢复记录要保留现场。[worker 执行状态](../../app/worker/state/execution.py)和[结果应用](../../app/controler/workflow/results.py)分别拥有这两侧证据。

**GitHub 写操作。** 数据库事务不能原子提交 GitHub 请求。control 先记录待执行操作；不知道写入是否成功时，开 PR 按 head 查找，评论按作者和稳定标记查找。查询未确认成功不等于原请求没有发出；显式 retry 也不能绕过这层保护。Webhook 的 202 只表示验签后收件已持久化；后台对账覆盖已追踪的 PR 和反馈，不从漏掉的创建事件推造 Flow。见[副作用恢复](../../app/controler/workflow/effects.py)、[Webhook 消费](../../app/controler/events/webhooks.py)和[对账范围](workflows.md)。

## 谁能让代码运行，谁能读到什么？

- **入口与身份。** GitHub App installation 提供仓库身份与 API 权限；Webhook 签名校验、触发者 allowlist 和 reviewer 名单用途不同。控制台单独验签 Cloudflare Access JWT 并限制 owner；写请求还检查同源。收到有效 webhook 或被指定为 reviewer 均不等于获得控制台写权限。见[GitHub App 接入](github-app.md)和[控制台授权](../deployment/access.md)。
- **凭据与隔离。** control 按 assignment 签发限仓库、限时的 push capability；worker 向本次进程提供 Git helper 和临时 `gh` 环境，不下发 App 私钥。skill 仓库同步单独使用只读授权。路径/TCP 策略可描述限制，但 Codex 和 Amp 目前均拒绝受限 profile；专用账户、容器或 VM 才是可信主机边界，工作区目录本身不隔离恶意代码。见[签发](../../app/github/repositories.py)、[凭据注入](../../app/worker/execution/credentials.py)、[权限检查](../../app/worker/execution/permissions.py)。
- **会话与输入。** Codex rollout 或 Amp thread 保留在原 worker。control 按 Flow/Attempt 和 session 代次查询、分页；数据库中的展示快照不能替代原始会话。控制台追加的任务按请求 ID 去重，在可安全派发时随任务交付，不保证能打断当次生成。见[会话查询](conversation-api.md)和[输入记录](../../app/persistence/agent_inputs.py)。
- **执行预算。** 编码超时限制的是连续没有 stdout/stderr 的时间；持续输出的进程仍可能长时间运行。control 另设 heartbeat、租约、修订次数和推理时限。它们不是按 Flow 计费的 token 预算。见[worker 配置](../../app/worker/config.py)、[空闲计时](../../app/worker/execution/idle.py)和[control 配置](../../app/controler/config.py)。

## 哪些保证目前没有？

| 边界 | 当前处理方式 |
|---|---|
| control 高可用、跨 worker 迁移 | 单一 serve 协调；原 worker 丢失后不能把未知执行交给另一台机器重跑。恢复依据与人工处理见[运维手册](../operations/runbook.md)。 |
| 不可信租户隔离、自动解冲突 | 不把策略模型视为已强制的沙盒；不自动 stash、强推或解冲突。受限后端和可迁移 checkpoint 仍是[后续设计](roadmap.md)。 |
| 父子 Flow 协作 | 自引用字段和仓库内搜索已存在；持久派生、等待和父级验收仍是[目标方案](derivable-flows.md)，不能作为可调用 API。 |
| 真实服务验收 | 本地及 CI 测试可验证状态和协议。GitHub App、真实模型、Access 登录、目标集群与故障恢复的证据以[验收矩阵](../verification/m5-live-e2e.md)为准，不从替身测试推断。 |
