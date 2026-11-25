# AgentFlow 还能怎样扩展？

今天可以让一个 Flow 经历多轮评审，也可以让一个 worker 同时承接不同 Flow。前者靠同一次开工沿用分支和 PR，后者靠不同工作区分开执行。两者都没有引入父子任务。先看[架构图](diagram.html)了解一次交付经过哪些系统，再从[功能走查](../feature-walkthroughs/README.md)定位当前行为和测试。

<a id="8-当前目录与里程碑"></a>

## 现有设计必须守住什么

**交付归一次开工。** Flow ID 标识任务，Attempt 标识一次开工，Job 标识一次可调度的工作。对评审意见作修订时应更新原 PR；已归档任务重新开工时，应能查出两轮各自交付了什么。工作端核验已推送的提交，控制端保存交付意图并核验结果后操作 PR。加一种编码后端也要走这条交付路径。见[领域对象](../../app/domain/models.py)、[首轮结果核验](../../app/controler/workflow/initial.py)和[评审循环](../feature-walkthroughs/review.md)。

**执行权不能靠超时转移。** 工作区绑定记录 worker 身份、目录、仓库和代次；任务凭据和连接代次约束谁能提交结果。同一身份恢复连接时可以核对本地执行记录。若旧任务可能已经运行，超过恢复期限只足以报告阻塞，不能凭此让另一台机器同时写入。见[任务租约](../../app/persistence/assignments.py)与[恢复判定](../../app/domain/recovery_policy.py)。

**重放有范围。** PostgreSQL 保存业务状态，Flow 生命周期的变化与对应事实同事务写入。[事实账本](flow-ledger.md)只足以重建 Flow 状态列；任务结果、已发送的外部操作和 GitHub 上的实际 PR，仍分别需要[结果记录](../../app/persistence/results.py)和[主动对账](../../app/controler/reconciliation/pull_requests.py)。

<a id="9-未来方向roadmap"></a>

## 演进方向及其前提

以下是候选方向，没有上线日期。每一项都需要说明现有记录不足以回答哪个问题。

| 方向 | 今天可以依赖什么 | 实现前还需要解决什么 |
| --- | --- | --- |
| 父子任务 | 每个 Flow 有独立标识；现有工具可以在所属仓库搜索 Flow。 | 谁授权派生？结果交给谁验收？父任务退出后谁来唤醒？见[派生编排提案](derivable-flows.md)。 |
| 跨机器恢复、高可用控制端 | 同身份重连、持久结果和原工作区可用于恢复。 | 为仓库和会话建立可校验的 checkpoint；恢复前撤销旧执行权。多个控制端还要协调调度权与连接归属。 |
| 隔离和交互 | 每个 Flow 分开工作目录；控制台可持久排队追加指令。 | 目录无法隔离同一账户下的不可信进程。每任务容器或微 VM、限制出站网络需单独设计；实时插话还需要编码后端确认何时接收指令。 |
| 审计和插件 | 生命周期已有事实记录；[合并后总结插件](../../app/plugins/builtin.py)收到事件会写日志。 | 若要重放交付及父子关系，先定义对应事实和版本；若总结插件要修改仓库 skill，需要提交可审核的变更并处理重复通知。 |

## 怎样读历史计划和验收记录

[M0 骨架](../plan/m0-closeout.md)记载早期 SQLite 设计；[首轮交付](../plan/m1-issue-to-pr.md)、[评审修订](../plan/m2-revision-delivery.md)、[归档](../plan/m3-archive-status.md)和[对账](../plan/m4-stage.md)解释今天这些边界从何而来。计划对应当时的问题；判断现在支持哪种操作，回到[功能走查](../feature-walkthroughs/README.md)。

[部署与运维阶段](../plan/m5-stage.md)提供模板和工具。真实 GitHub App 授权、模型执行、Access 登录及 MFA、目标集群和备份恢复，仍须在目标环境记录结果。核对[真实闭环清单](../verification/m5-live-e2e.md)和[运维手册](../operations/runbook.md)，不要把自动化测试当作这类验收的凭据。
