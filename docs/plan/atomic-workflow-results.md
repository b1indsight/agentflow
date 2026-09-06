# Flow 事务边界与结果副作用恢复

> 状态：已批准并实施；验证范围见下 · 计划/批准日期：2026-09-06 · 对应架构审查第 1 项
> 基线：`main` 的 `bbf99f98`（PR #16）；本 PR 从计划到实现使用同一 bookmark。

## 目标与问题

将一次流程操作涉及的本地状态、派生任务和结果确认作为一个原子提交单位，关闭结果重放产生重复任务
的窗口；GitHub 操作通过持久执行记录恢复，避免已完成的评论在重放时再次发送。

当前存在两个独立的提交间隙：

- `ControlService.on_trigger()` / `retry()` 分别提交 Flow 状态和 job；中断可能留下没有任务的活跃 Flow。
- Gateway 先调用 Coordinator 修改本地状态、发送评论、派生 job，再调用 Store 写入
  `result_applied_at`。在两者之间中断后，持久结果会重新执行业务逻辑。

只读审查使用现有测试替身复现：同一 TRIAGE 结果应用两次，会产生两条 REVISION job 和两次评论调用。
既有 result inbox 已解决结果接收去重，PR #16 已实现 INITIAL 成功前禁止开 PR；本次在这些约束上补齐
本地事务和外部副作用的恢复边界。

## 核心决策

### 1. 显式共享一个数据库事务

Store 增加显式、连接绑定的工作流事务入口，事务内的方法共享同一 PostgreSQL connection，复用现有
SQL 和模型校验。事务结束后连接归还连接池，不将连接隐藏在跨异步任务传播的全局上下文中。

以下操作分别成为完整事务：

- trigger：确认 active 唯一性、创建 Flow、创建 INITIAL job；并发重复触发返回已有结果。
- retry：校验当前 Flow 和最近失败 job、恢复状态、创建相应 job；并发请求在锁内重新校验。
- merge / cancel / failure：相关 Flow、delivery、execution binding、job 变更一起提交。
- terminal result：应用领域变更、写入 run、创建派生 job、写入 `result_applied_at` 和 job 终态，
  并按当前连接状态释放 worker slot，一起提交。

事务按统一顺序锁定涉及的 Flow、worker、job，并与现有 assign/start/disconnect/sweep 的锁序核对，
避免引入反向加锁。状态写入使用锁内读取的新值，不能用网络调用前的旧 Flow 对象覆盖 cancel 或 failure。
调度通知和插件 hook 在提交后触发；插件仍保持现有尽力执行语义，不纳入 GitHub 交付保证。

### 2. 保持结果确认在必要的 GitHub 操作之后

不把 GitHub 请求放在数据库事务内，也不在 PR 尚未交付时提前返回成功。结果处理分为三个阶段：

1. **准备**：复用现有持久 terminal inbox；在短事务中校验 result、读取当前 attempt/delivery，
   冻结本次应用所需上下文及有序的副作用参数。记录与 `job_id + result_digest` 绑定，重放复用它。
2. **执行**：提交准备事务后执行持久副作用，逐项保存远端结果；重启从未完成项继续。
3. **完成**：必要副作用成功后，在一个事务中提交全部本地变更、派生 job 和结果应用标记。
   提交成功才发送 `ResultAccepted`。确定不可恢复的业务失败也通过这一入口原子收尾。

派生 job 以来源 job 和派生用途作为唯一键；run 记录同样关联来源 job。数据库约束与事务共同防止
在线连接、启动恢复和丢失 ACK 后重放重复生成记录。已有普通 job/run 数据允许没有来源键。

Gateway 的在线结果和 `recover_pending_results()` 共用同一个结果处理入口，不再由调用方在业务应用
之后独立调用 `finish_assignment()` / `complete_persisted_result()` 完成确认。尚未执行 worker task 的
prepare 失败、executor 丢失等控制面终态，也使用相同的原子收尾规则。

已有持久 terminal result 由 control 完成恢复，不能因旧连接离线被 lease sweep 重新执行或提前覆盖。
收到旧 epoch 的新消息仍然拒绝；恢复已接受结果时使用已存 digest 和当前领域状态，不要求旧 socket 在线。
取消后禁止待处理的成功交付继续改变 Flow；已有远端结果保留记录，不尝试删除 PR 或评论回滚。

### 3. 最小持久副作用账本

新增针对当前 GitHub 操作的持久记录，包含：来源 job/digest、稳定 effect key、操作类型、冻结参数、
执行状态、远端结果、重试时间和最后错误。使用类型明确的 payload，限定为现有 PR 交付、评论、标签、
reviewer 请求和 thread resolve；它不是通用工作流引擎。

同一 job 的必要操作按顺序执行，先保存依赖操作的远端结果，再执行后续操作。claim 使用数据库条件更新
并保存执行身份；完成或重试也校验该身份。重复唤醒只产生一次有效执行，进程重启恢复未完成记录。

- **PR**：保留 `flow.open_pr` 的 prepared ledger 和 INITIAL terminal 校验；随后按既有稳定 head
  查找/创建并校验 PR，保存其编号和 SHA，再完成 label/reviewer。不能恢复“INITIAL 失败也开 PR”的行为。
- **评论**：使用来源 job 和评论用途生成稳定标记，附于评论的隐藏 HTML 注释中。恢复时分页查询本工具
  作者的精确标记并复用评论；远端 ID 写入账本。明确未执行的失败可以重试创建。
- **标签、reviewer、thread**：查询远端目标状态后补齐缺失操作；reviewer 判断同时考虑已经提交的
  review，避免在同一结果恢复时重复请求已完成的审核。

对“请求可能已被 GitHub 执行，但响应丢失”的情况，记录为待核对，先查询远端事实；没有可确认结果时
不得直接重发非幂等 POST。未决记录继续可查询、可诊断，必要时等待人工确认。这是可恢复和防重复的
明确边界，不宣称 PostgreSQL 与 GitHub 具有跨系统 exactly-once 事务。

Serve 启动和运行期间都扫描到期的未完成记录。可重试错误使用有上限的退避，尊重适配器现有 retry
metadata；一个 job 的错误不能终止整个恢复循环。测试注入 clock 和唤醒，不依靠真实轮询等待。
`flow show` 至少展示 pending / 待核对的结果操作及原因，显式 retry 不能绕过仍未解决的副作用记录。

## 文件与模块

- `app/store.py`：连接绑定事务入口、事务化流程用例、结果最终提交、派生来源唯一约束，以及最小增量 schema。
- 新增 `app/result_store.py`：结果准备记录与副作用账本的持久化，复用 Store 的连接/事务边界。
- `app/models.py`：结果应用上下文和副作用的类型化模型。
- `app/control/service.py`：trigger/retry/merge/cancel 调用事务用例，提交后通知。
- `app/control/coordinator.py`：明确区分准备结果、GitHub 操作和本地提交，保留当前 JobKind 的领域判断。
- 新增 `app/control/effects.py`：执行、核对和重试持久 GitHub 操作，独立管理其运行生命周期。
- `app/control/tools.py`：复用 PR prepare 和远端验证，让 PR 交付结果参与最终本地提交。
- `app/control/gateway.py`、`app/control/runtime.py`：统一在线/恢复结果入口，接入持续恢复及关闭清理。
- `app/ports.py`、`app/github_client.py`、`app/github_models.py`：增加评论和必要远端状态查询契约。
- `app/cli.py`：展示未完成的结果操作；更新相关架构当前事实和本计划实施记录。

继续使用当前同步 Psycopg 和单 serve 拓扑；schema 只做本项需要的增量扩展。Webhook inbox、settle 持久化、
反馈批次消费、迁移框架和异步数据库改造属于其余审查项，不合并进入此 PR。

## 实施顺序与验证

1. 先添加最小失败回归：trigger/retry 中途异常、TRIAGE 重放重复派生，以及远端评论成功后本地确认中断。
2. 实现共享事务与流程命令的原子提交；用真实 PostgreSQL 验证 rollback、唯一约束和跨连接并发。
3. 实现结果准备记录和副作用账本，再接入 GitHub 查询/执行，验证模糊失败不会盲目重复发送。
4. 将五种 JobKind 和控制面失败路径接入统一提交入口；在线结果、重连与启动恢复共用实现。
5. 接入持续恢复、生命周期清理和必要的 CLI 状态，同步文档。
6. 运行定向回归，再运行完整 pytest（包含 PostgreSQL）、Ruff、Mypy、build 和代码审查门禁。

关键故障场景及验收：

| 场景 | 必须满足的结果 |
|---|---|
| Flow 创建或 retry 的事务中注入异常 | 状态和新 job 一起回滚，不留下半完成状态 |
| 相同持久结果由在线和恢复入口并发应用 | 本地变更、run、派生 job 只提交一次 |
| 副作用准备后、发送前中断 | 重启执行已有记录，参数和 effect key 不变 |
| 评论已创建、远端 ID 尚未落库时中断 | 找回带标记的原评论，不创建第二条 |
| 请求超时且无法确认是否创建评论 | 保留待核对状态，不重复 POST，不伪报完成 |
| 副作用成功、最终数据库事务失败 | 重放复用副作用结果，重新完成本地原子提交 |
| 最终事务提交后 ACK 丢失 | 重发 ResultAccepted，不重复副作用或派生 job |
| result 等待交付期间 worker 重连、旧 epoch 到达或 sweep 执行 | 拒绝旧消息，保留有效结果，不重跑 worker task |
| cancel 与结果收尾交错 | 不用旧快照复活 Flow，不继续已取消的成功交付 |
| INITIAL 失败或 PR 已关闭 | 保留 PR #16 的拒绝行为，不提前或重复创建 PR |

GitHub 使用有状态测试替身模拟“远端提交成功后抛出连接错误”，不能仅以调用次数模拟未执行的请求。
真实 PostgreSQL 用例验证事务与重启恢复，内存替身不能代替这一验收。用户已要求本轮先跳过本机真实
PostgreSQL 测试和安装；保留集成用例，并在交付时明确实际执行范围。现有 CI 提供 PostgreSQL 服务。
不访问真实 GitHub 仓库，也不运行真实 Codex/bub。

验证命令沿用仓库：`uv run pytest`、`uv run ruff check app tests`、`uv run mypy app tests`、
`uv build --no-sources`、`git diff --check`；代码通过工作流审查门禁后推送同一 PR。

## 兼容与交付边界

本版本接收的未应用 terminal result 在首次应用时创建处理记录；已经 applied 的历史结果保持原样。旧的评论没有
稳定标记，因此无法自动证明升级前未知窗口中的某条评论是否已经发送；这类记录必须显式核对，不能
把标记缺失解释成允许重发。新增记录不存放 GitHub token 或私钥。

部署时停止旧 serve，再启动新版本恢复未完成结果；不支持新旧 serve 混跑。回退前需要处理完或明确
隔离新增待办，不能直接让旧版本忽略账本并重放相同结果。实现和代码审查完成后再标记本计划已完成。

## 实施记录（2026-09-06）

- 新增连接绑定的 Store 事务视图；trigger/retry/merge/cancel、PR prepared intent 和最终结果各自共享一个提交边界。
- 结果应用分为持久接收、冻结计划、远端操作回执、最终本地提交；线上消息和后台持续恢复走同一路径。
- 派生 job/codex run 增加源 job 唯一约束；评论分页核对本 bot 的隐藏 marker，未知非幂等结果不盲目重发。
- 取消保留 running executor 的结果接收能力，过期清理不覆盖 CANCELLED；已接收的结果不再交回 worker 执行。
- 回归覆盖响应丢失、进程中断、最终提交失败、并发应用、取消交错、限流、旧结果阻止重放和 ACK 前置条件。
- 本机：`uv run pytest -q -p no:cacheprovider` 为 **118 passed / 30 skipped**；Ruff、Mypy、CLI help、
  `uv build --no-sources` 与 `git diff --check` 通过。一个既有 Starlette/httpx 弃用告警保留。
- 用户要求的“真实测试 2 先跳过”按本机 PostgreSQL 测试/安装暂停执行；未访问真实 GitHub/Codex/bub。
  30 个数据库用例包含 SQL 回滚、跨连接触发、领取/取消互斥、源任务去重和 recovery deadline，用现有 CI 验证。
