# AgentFlow 还能怎样扩展？

今天可以让一个 Flow 经历多轮评审，也可以让一个 worker 同时承接不同 Flow。前者靠同一次开工沿用分支和 PR，后者靠不同工作区分开执行。两者都没有引入父子任务。先看[架构图](diagram.html)了解一次交付经过哪些系统，再从[功能走查](../feature-walkthroughs/README.md)定位当前行为和测试。

<a id="8-当前目录与里程碑"></a>

## 现有设计必须守住什么

**重新开工：同一个 Flow，新的 Attempt。** 修订沿用本次交付的分支和 PR，见[评审循环](../feature-walkthroughs/review.md)。重新开工则由[控制台命令](../../app/controler/console/api.py#L299)调用 [`ControlService.resume`](../../app/controler/workflow/service.py#L189-L227)，传入 Flow 和用户所见的 `expected_attempt_id`。锁内要求该 Attempt 仍是当前轮次、已归档且有归档记录、原绑定已释放，同一 Issue 也不能已被另一 Flow 占用。通过后重置本轮同步和对账进度，以 [`transition(ATTEMPT_STARTED)` 创建新 Attempt](../../app/persistence/flows.py#L143-L155)，有旧交付时为新轮次预留不同分支，然后排新的 INITIAL Job。过期或重复的开工请求不能再开一轮。加入新编码后端也必须维持[首轮交付核验](decisions.md)。见[归档后开新 Attempt 的测试](../../tests/test_console_api.py#L765)。

**调度：哪个目录可以写？** [worker 会话申请工作](../../app/controler/workers/callbacks.py#L330)时，以 worker ID、token 和连接代次调用 [`AssignmentStore.assign_job`](../../app/persistence/assignments.py#L89-L107)。执行槽的 [`_assign_coding`](../../app/persistence/assignments.py#L141-L194)只看本连接上报、匹配仓库且空闲的工作区；已有绑定时还得匹配原目录和代次。只有 INITIAL 能首次占用未绑定的目录，且受排空状态和容量上限约束。锁内复查后写绑定并认领 Job；没有合格位置就不认领。一个 worker 上不同 Flow 因此可以并行，已有绑定的 Flow 不会随意换目录。见[并发分配与停止互不干扰的测试](../../tests/test_workspace_dispatch.py#L85)。

**断线：何时能接回原执行？** [重连处理](../../app/controler/workers/callbacks.py#L986-L998)收到 `started` 却没有活跃执行器的报告，会保留普通编码 Job；只有可恢复的归档清理例外。[`resume_assignment`](../../app/persistence/assignments.py#L357-L409)按原 Job、worker、token、当前连接和工作区身份核验，然后才续租。[工作端 `ExecutionState.resume`](../../app/worker/state/execution.py#L148-L186)也要能接回原执行，或从冻结的归档任务恢复清理。超时时，[清理过期分配的调用处](../../app/persistence/assignments.py#L748)使用 [`decide_job_recovery`](../../app/domain/recovery_policy.py#L61-L77)：已收到结果就保留；租约过期、能证明未执行且无待停止请求才重排；可能执行过的任务超过恢复期限则受阻。这些条件没有一个授权把有疑问的写入搬到另一台机器重跑。

**审计：重放能证明什么？** [`FlowStore.transition`](../../app/persistence/flows.py#L123-L175)要求调用者已开启事务；旧状态或 Attempt 不符就拒绝写入，受条件保护的空操作不留新事实。有效变化和相应事实同事务保存。[`FlowStore.verify`](../../app/persistence/flows.py#L209-L232)锁定要比对的 Flow，用持久正文和事实调用 [`replay_flow`](../../app/domain/flow_policy.py#L117-L148)：缺起点、正文摘要不符或重复开始同一 Attempt 都报错。输出是账本负责的列与当前行的差异，不会修改 Flow，也不会查 GitHub。PR 是否实际合并仍靠[主动对账](../../app/controler/reconciliation/pull_requests.py#L183)，已发评论能否恢复则靠[逐项回执](decisions.md)。见[非法迁移回滚测试](../../tests/test_flow_states.py#L92-L106)和[事实账本](flow-ledger.md)。

<a id="9-未来方向roadmap"></a>

## 演进方向及其前提

以下是候选方向，没有上线日期。每一项都需要说明现有记录不足以回答哪个问题。

| 方向 | 今天可以依赖什么 | 实现前还需要解决什么 |
| --- | --- | --- |
| 父子任务 | 每个 Flow 有独立标识；现有工具可以在所属仓库搜索 Flow。 | 谁授权派生？结果交给谁验收？父任务退出后谁来唤醒？见[派生编排提案](derivable-flows.md)。 |
| 跨机器恢复、高可用控制端 | 同身份重连、持久结果和原工作区可用于恢复。 | 为仓库和会话建立可校验的 checkpoint；恢复前撤销旧执行权。多个控制端还要协调调度权与连接归属。 |
| 隔离和交互 | 每个 Flow 分开工作目录；控制台可持久排队追加指令。 | 目录无法隔离同一账户下的不可信进程。每任务容器或微 VM、限制出站网络需单独设计；实时插话还需要编码后端确认何时接收指令。 |
| 审计和插件 | [合并事务提交后通知插件](../../app/controler/workflow/service.py#L298-L307)；[`PostMergeSummary.on_merged(flow)`](../../app/plugins/builtin.py#L15-L21)仅记录 Flow ID，无仓库写入。 | 若要重放交付及父子关系，先定义对应事实和版本；若总结插件要修改仓库 skill，需要提交可审核的变更并处理重复通知。 |

## 怎样读历史计划和验收记录

[M0 骨架](../plan/m0-closeout.md)记载早期 SQLite 设计；[首轮交付](../plan/m1-issue-to-pr.md)、[评审修订](../plan/m2-revision-delivery.md)、[归档](../plan/m3-archive-status.md)和[对账](../plan/m4-stage.md)解释今天这些边界从何而来。计划对应当时的问题；判断现在支持哪种操作，回到[功能走查](../feature-walkthroughs/README.md)。

[部署与运维阶段](../plan/m5-stage.md)提供模板和工具。真实 GitHub App 授权、模型执行、Access 登录及 MFA、目标集群和备份恢复，仍须在目标环境记录结果。核对[真实闭环清单](../verification/m5-live-e2e.md)和[运维手册](../operations/runbook.md)，不要把自动化测试当作这类验收的凭据。
