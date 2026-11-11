# M3 Feature 2/3：工作区清理与 worker 释放

> 状态：用户于 2026-09-08 指示实施 PR #25；已实施，待代码审查。
> 规划基线：`main` / `81417c5b`；bookmark：`feat/m3-workspace-archive`。
> 实现前置：[F1 合并检测与任务收敛 #24](https://github.com/b1indsight/agentflow/pull/24)。
> 计划 PR 以 main 为 base；F1 实现并合并后更新本分支，在同一 PR 实施。

## 1. 目标与边界

F1 确认合并并停止后续编码；F2 让原 worker 清理该 Flow 的工作区，control 收到可核验凭据
后完成归档并释放 worker。删除失败、断线或 ACK 丢失都不能把错误 worker 的空目录当成功。

当前 `ArchiveTask` 只有 Flow/attempt/repo，`ArchiveResult` 为空；worker 调用
`cleanup_flow_repo()`，control 随即设 SUCCEEDED 并释放 binding。通用 failure/recovery
还可能在清理完成前释放 binding。F2 只增强这一条资源生命周期，不处理 GitHub 标签/评论。

复用 jobs、FlowExecution、result inbox/ResultPlan 和 worker journal。不新增归档表、
持久归档阶段枚举或第二套 receipt 系统；阶段由 job 状态、清理结果、结果应用标记推导，
错误复用 job.last_error。F3 在同一结果处理路径增加状态回写。

## 2. 方案

### 2.1 最小清理契约

- ArchiveTask 增加冻结的 worker/repo identity、delivery version；沿用现有 Flow/attempt
  和 assignment token/epoch。control 必须确认 delivery MERGED、绑定仍属于原 worker，
  F1 的旧 executor/结果已收尾，才允许派发。
- ArchiveResult 返回对应 Flow/attempt/repo identity、delivery version 和清理事实
  `removed` / `already_absent`。control 逐项校验并保存在现有 result inbox/plan；
  不接受空结果或仅靠另一 worker 的目录不存在判断完成。
- worker 从配置和 Flow ID 派生目标路径，不接受远端任意绝对路径。清理不调用 CodeAgent、
  Judge 或 GitHub，也不需要 push token。
- 扩展归档字段时只升级一次协议（基线 v7 → v8），serve/worker 同步升级。旧空结果不得
  补造凭据；部署先排空旧归档任务/未应用结果，无法排空则人工核验。

### 2.2 清理与崩溃

只删除受管 Flow repo 及同一规则可明确归属的临时目录。验证目标不同于 seed/root/其他
Flow，拒绝顶层 symlink 和路径逃逸；内部 symlink 只删除链接，不跟随其目标。
保留 seed、其他 Flow、数据库历史、worker journal 和共享 Codex 会话；不删除远端 branch。

开始删除前在现有 journal 保存目录归属已核验的清理标记，保留到清理结果确认。目录已不存在
只有在这个标记存在时才是幂等成功；首次看到空目录且无标记应阻塞，不把更换 root 当成已清理。
部分删除可在原 worker 重试。成功先写 journal，再发送 result；ACK 前保留 journal，已有
终态只重放。删除后、写终态前崩溃时，仅在原 worker/root 且确认旧 executor 已结束后重试
幂等清理，不扩展通用 started job 重跑策略。更换 root 或丢失 journal 需人工处理。

### 2.3 失败与释放

清理已验证 → 持久冻结结果 → 同一事务完成 ARCHIVE、Flow SUCCEEDED、binding 释放和
result applied → ACK。F2 阶段没有 GitHub effects；F3 将在最后提交之前追加必要回写。
成功归档时间使用 job 的持久完成/应用时间，不再加一套归档时间字段。

只对 MERGED 且归档未完成的 Flow 调整 failure/recovery/cancel 路径：保留原 binding，
阻止其他 worker 接管；未知 executor 状态或原 worker 长期离线显示阻塞，不推断已经清理。
普通未合并 Flow 的通用恢复不在本 feature 的改造范围。

扩展现有 `flow retry`，仅允许 ACTIVE、MERGED、原 binding 完整且无正在执行/未应用结果的
Flow 重试失败 ARCHIVE。保留当前 attempt，不创建 INITIAL；重新派发也只给原 worker。
有未应用清理结果时交给既有恢复循环，不重做删除。进程停止无法确认时拒绝重试并说明原因。
历史 FAILED/CANCELLED Flow、已释放 binding 和跨 worker/root 恢复留待人工处理。

`flow show` 基于现有 job/result 展示等待、清理失败、已清理/待提交或完成及错误；修正
`list_flows(active_only=True)` 的历史 archived 字符串，使 SUCCEEDED 不出现在 active 列表。
保持 `on_archived` 的现有提交后尽力调用语义，不实现持久插件投递。

## 3. 落点与实施顺序

| 落点 | 改动 |
|---|---|
| `app/protocol.py`、`worker/executor.py`、`app/vcs.py` | 版本化清理凭据、身份/路径校验、幂等删除 |
| `app/worker/journal.py`、`client.py` | 仅在现有恢复接缝不足时补归档重放和原 executor 退出检查 |
| `app/control/coordinator.py`、`results.py`、`app/result_models.py` | 派发校验、清理结果冻结、原子完成 |
| `app/store.py`、`control/service.py`、`gateway.py` | 归档专属 binding 保留、失败重试、active 查询 |
| `app/cli.py`、README/配置升级说明 | 派生进度显示与有限恢复说明 |

审批后先用真实临时目录/repo 写清理边界测试，再实现协议和 worker；然后补 PostgreSQL
资源释放/恢复测试并接入 control，最后做 worker 断线及 ACK 重放集成测试和文档。

## 4. 验收

- 正常删除、已不存在、部分删除、权限失败、顶层/内部 symlink；seed、其他 Flow、journal
  保留。使用真实临时 Git/jj repo，不只断言 cleanup 函数被调用。
- 错 worker/root/repo/attempt、旧 token/epoch、digest 冲突：拒绝结果，不释放他人资源。
- 删除前后崩溃、journal 写入后断线、serve 重启、ACK 丢失：原身份恢复，已成功结果只重放。
- grace 到期/旧进程仍未知：不清理、不释放、不派给别的 worker；原 worker 恢复后可按上述
  条件 retry archive，绝不启动编码。历史终态和已释放 binding 不自动恢复。
- 原子完成事务回滚/重放：Flow 成功、job 完成、binding 释放同成同败；成功后第二个 Flow
  才能使用该 worker，active 列表不再显示旧 Flow。

数据库并发与恢复测试用真实 PostgreSQL。实现完成运行 `uv run ruff check app tests`、
`uv run mypy app tests`、完整 pytest（配置数据库，无数据库跳过）和 `uv build --no-sources`。
F2 单独验收真实本地 worker/目录的清理与复用；GitHub 回写及完整 M3 验收归 F3。
本计划阶段仅检查文档差异与链接，未运行产品测试。

## 5. 实施记录（2026-09-08）

在 #24 合并后将同一 bookmark 更新到 main，实现仍沿用 PR #25。

- v8 ArchiveTask 冻结在 job.payload；ArchiveResult 在 result inbox 与 ResultPlan 中持久保存。
  派发、结果准备和最终提交均检查原绑定、MERGED delivery 版本、当前 attempt 和旧任务收敛。
  job 完成、Flow SUCCEEDED、释放绑定和 result applied 在同一 PostgreSQL 事务提交。
- journal v2 增加本地 root 路径/device/inode 和归档任务/校验标记。为防止任务间更换 root，
  普通任务 ACK 后保留上一任务记录，成功归档 ACK 后删除；失败 ACK 与未 Started 的重新派发保留归属。
  此处扩大了原计划的 journal 保留时段，仍复用同一文件，未增加 receipt 系统或归档表。
- 仅删除派生 repo 和 `.partial`；清理前拒绝不安全路径，内部链接不跟随，取消等待删除线程退出。
  原 root 的进程锁使纯文件删除可在进程重启后继续；旧编码 executor 退出未知时不推断成功。
- MERGED/ACTIVE 归档失败保留绑定；grace 过期保留未决 executor，原 worker 可恢复结果。
  retry 仅对在线原 worker 的已结束失败 ARCHIVE 生效，保留 attempt；pending result 由恢复循环处理。
  合并后 cancel 明确拒绝，历史终态/丢失 journal/已释放绑定需人工核验。
- `flow show` 显示派生归档进度，修正 active 列表；on_archived 保持提交后尽力调用。
  README 记录 v8/journal v2 的排空与人工核验要求，旧空结果不补造清理凭据。

验证覆盖真实 Git/jj 工作区、PostgreSQL、symlink/缺目录/错身份/版本冲突、权限失败与部分删除、
删除线程取消、删除前后崩溃、终态 journal、serve 重启、ACK 丢失及第二个 Flow 复用。
本次仅验证本地 worker 与 control；GitHub 状态回写及真实 GitHub/Codex/bub 全闭环归 F3。

最终验证：真实 PostgreSQL 下完整 pytest **461 passed / 0 skipped**；Ruff、Mypy（88 个源文件）、
`uv build --no-sources` 的 sdist/wheel 均通过。保留已有 Starlette TestClient/httpx 弃用告警。

### 审查修复

首轮审查的 1 High / 4 Medium 已按用户指示修复：

- 领取后、冻结前崩溃的 unstarted ARCHIVE 不再被误判为旧协议；真正已开始却缺少冻结任务的
  旧归档及旧协议未确认结果仍拒绝升级。
- 重连前将已完成的活跃 executor 结果写入 journal，复用正常完成时的凭据脱敏与结果构造；
  journal 写入失败时保留内存中的完成结果，不把 done task 误判为退出未知。
- 拒绝 received 状态的普通任务时保留 root/Flow 归属，合并后可接续 ARCHIVE。
- 冻结归档视作可能已执行，断线/serve 重启及 lease sweep 均保留原分配；恢复时核对 token、epoch、
  原绑定和完整冻结任务，再补记丢失的 Started 并恢复清理或结果重放。普通编码不获得此重跑能力。
- 重试只检查 queued ARCHIVE；合并后恢复产生的过期编码队列项仍不能被执行，也不阻塞清理重试。

新增 17 项回归覆盖上述窗口，其中真实 Git/jj 目录验证 Started 丢失后的超时恢复；
PostgreSQL 验证冻结身份/围栏拒绝，真实 worker/control 往返验证合并与重连交接。

## 6. 需求与方案修订记录

### [001] — 2026-11-11 — PR 合并后业务归档，工作区由 Worker 回收

**需求已明确；以下技术方案待审批，尚未实现。** 保留上文原始方案及其实施记录。本条拟替代第 1、2.3、4 节中合并归档必须等待工作区清理、原执行绑定释放，以及历史已释放绑定只能阻塞归档的约定；未合并 Flow 的结束规则保持不变。

#### 背景、范围与目标

用户在[本次讨论](https://ampcode.com/threads/T-01a1c6c5-ea85-70e7-baf4-76ca5604a43b)明确要求：“pr合并后正常归档，工作区靠worker自己清理”。用户提供的数据库输出显示，Flow 4 的 PR 已合并、`archive_reason=succeeded`，但原 Worker 绑定在合并前已经释放，Flow 与控制台均停在 `archive_blocked`。绑定最初释放的原因尚未查明。Flow 3 的 PR 仍为 open，属于执行失败，不应通过本次修复改成成功。

当前 [`record_merge`](../../app/controler/workflow/lifecycle.py) 在绑定缺失或已释放时设置 BLOCKED；[`try_archive`](../../app/persistence/flows.py) 同时等待停止确认、旧结果、清理回执和绑定释放；归档通知又依赖 Worker 返回 ArchiveResult。因此只移除清理回执检查不能完整解决问题。

目标是：Control 确认当前轮次的 PR 已合并后，提交 `ARCHIVED / SUCCEEDED` 和归档活动记录。原 Worker 离线、停止未确认、清理失败、绑定已释放，以及通知发送失败，都不改变这个业务结论。目录只有在 Worker 完成安全回收后才能重新分配。

规划依据为已 fetch 的 `main@origin`，同时核对了本地 `feat/worker-workspace-concurrency` 的 protocol 20、多工作区库存与逐 Flow journal 实现。本方案依赖这些位置与 generation 约束；实施时须先确认该并发工作的最终基线，不能覆盖其现有 smoke 修改。计划、代码、部署和生产积压修复是不同交付阶段，本次仅形成计划。

#### 方案取舍

- **保留当前前提或增加超时：不采用。** 原 Worker 永不恢复时仍会影响业务归档；超时也不能证明目录已清理。
- **只删除 cleanup guard：不采用。** 仍会被旧执行、绑定释放或通知路径阻塞；inventory 的代际交接还会拒绝已经归档的 Flow。
- **业务归档独立提交，资源复用现有停止回收流程：拟采用。** 不新增清理表、远端目录接管或通用任务系统。正常新合并不再派发 Worker ARCHIVE 作业，Worker 通过既有 `worker.stop` 停止执行并回收自己的目录。
- **通知成功后才归档：不采用。** 评论权限或网络故障不应让已经合并的 Flow 重新成为业务失败；通知作为独立、可重试的 Control 工作保留。

#### 三个独立提交边界

1. **业务归档事务。** webhook 与 PR reconciliation 继续共用 `record_merge`。在原 workflow 锁内核对 repo、PR、轮次与合并事实，保存 merged_at / merge_commit_sha，取消尚未执行的编码意图，保存必要的停止请求和归档通知意图，追加唯一的本轮 archive 活动，最后更新为 ARCHIVED / SUCCEEDED。上述数据库写入同成同败，不等待网络、Worker 回执或资源绑定释放。重复事件不重复归档；旧轮次事件继续走既有历史更正路径，不归档新的轮次。
2. **Worker 资源回收。** 活跃原绑定继续保留真实目录的独占，既有 StopStore 记录原 job、token、workspace、generation 和 binding 时间。原 Worker 在线或重连后执行停止与本地回收；停止、ACK 和 inventory 仍按原顺序持久化。确认后再释放资源绑定并允许下一代库存参与派发。业务已归档不等于目录空闲，不能清空 `current_job_id`、伪造停止确认或提前推进 generation。
3. **Control 归档通知。** Control 从持久化通知意图发布既有 Issue / PR 标签与评论，保留幂等标记、发送结果不确定时的查询恢复和缺项重试。通知错误记录在通知作业及 effects 中，Flow 始终保持已归档。新正文仅陈述 PR 已合并、本轮已归档、工作区由原 Worker 异步回收，不再声称“工作区处置已确认”。

业务归档不再要求 `release_flow_execution` 返回成功。该方法的停止、未应用结果及副作用核对保留，用于保护资源。已归档后的结果应用、停止确认与 inventory 结清要继续尝试资源释放，而不能因 `try_archive` 对终态返回 False 就停止收尾。

#### 数据与接口设计

以下名称和签名均为拟议接口。复用 jobs、result inbox、ResultPlan、result_effects、StopStore 与本地 manifest / journal，不新增数据库表或 Worker wire 消息。

新增一个仅用于 Control 持久化的 `MergeArchiveNotice`，不加入 Worker 可发送的 ExecutionResult。它同时作为通知收件与 ResultPlan 的冻结依据，避免再增加一层成功结果包装。

| 字段 | 类型 | 含义 |
|---|---|---|
| `type` | `Literal["merge_archive"]` | 与 Worker 执行结果区分的持久化类型。 |
| `job_id` | `int` | 唯一拥有此次通知的本地作业。 |
| `flow_id` | `int` | 归档所属 Flow。 |
| `round_no` | `int` | 归档所属轮次，不能从当前轮次推断。 |
| `repo` | `str` | 经过既有入口校验的仓库身份。 |
| `pr_number` | `int` | 本轮已经合并的 PR。 |
| `merged_at` | `datetime` | 已验证并持久化的合并时间。 |
| `merge_commit_sha` | `str` | 已验证并持久化的合并提交。 |

- `JobStore.record_merge_notice(flow: Flow, delivery: FlowDelivery) -> Job`：在业务归档事务中创建本地 ARCHIVE 通知作业及冻结收件。该作业没有 assigned_worker_id、workspace、generation 或 assignment token，不进入远程领取队列；其后复用现有结果恢复驱动。
- `decode_result_inbox(...) -> ResultInbox`：扩展明确版本的持久化 reader，保留旧收件只读兼容。新增通知类型只允许 `source=control` 且匹配未分配的本地 job；不能把 Worker ArchiveResult 的 cleanup 改成可选，也不能使用假 token 或伪造清理成功。
- `ResultPlan.archive_notice: MergeArchiveNotice | None`：区分业务通知与旧 `archive: ArchiveResult | None` 清理证据。冻结通知使用现有 canonical JSON / SHA-256 方式核对 digest，旧结果与 digest 不重写。
- `verify_merge_notice(flow: Flow, job: Job, notice: MergeArchiveNotice, *, activity: ActivityStore) -> None`：对照不可变归档活动验证轮次、仓库、PR 与合并事实，不要求 Worker 在线或绑定仍活跃。
- `WorkerStore.release_flow_execution(flow_id: int) -> bool`：保留现有签名和严格资源前提，由结果应用和精确 inventory 结清路径直接调用，不新增 FlowStore 包装。资源释放失败仅保留占用；本地通知完成不得更新 Worker 的 lease、recovery deadline 或当前 job。
- `ReconcileStore.merged_backlog(limit: int) -> list[int]`：有界选取已保存完整合并事实、当前轮次尚未归档的 Flow，由 reconciler 在 workflow 锁内重检并调用同一业务归档入口；不依赖新 webhook，也不重新激活绑定。

[`ResultApplication`](../../app/controler/workflow/results.py) 与 [`ResultEffects`](../../app/controler/workflow/effects.py) 需要在通用终态短路前识别合法 Control 通知。只给经过归档活动验证的通知开放发送通道；普通 INITIAL / REVISION 结果继续按 retired 处理，不能借此恢复编码、请求 review 或修改交付。

复用原归档 effects 的幂等身份。旧通知已经派发但结果未知时，先恢复原 marker / receipt，不能同时启动一份相同目标的新发送。轮次已 resume 或 Issue 的展示归属已改变时，不再发送会覆盖新一轮状态的旧标签；已派发操作仍按既有不确定结果规则核对。

#### 资源边界与兼容

- **执行中或 Worker 离线。** 业务先归档，原停止请求保持持久化；Worker 回来后继续停止与回收。不把进程“暂时看不见”当作退出证据，也不再允许已归档 Flow 获得编码授权。
- **清理失败。** 保留 Worker 本地释放标记、journal 与原目录隔离状态；正常停止恢复继续负责回收。失败只影响该位置的容量，不能阻止其他已核验位置接单，不能把 Flow 改回 BLOCKED。
- **ACK 丢失与 generation。** 保留原 job/token/digest 的回执重放。调整 inventory 的已归档分支：精确清理凭据允许释放旧资源，不再要求 FINALIZING，也不再为等待 ARCHIVE 将已清理目录交接回旧 Flow。旧 token、epoch、generation 或其他 Flow 的 ACK 不得释放新分配。
- **绑定已经释放或缺失。** 可以业务归档，不重建旧活跃绑定。收到可识别的旧目录库存时，只能根据原位置及代际的持久证据核对；不能将其当成新任务授权。缺失 journal、根目录或 generation 证据时隔离该位置并报告人工核验要求，不以另一 Worker 的空目录补证，也不因此拒绝其他有效位置的正常注册。
- **显式 resume。** 本次保留原资源绑定未释放时拒绝 resume 的保护。用户仍能看到已归档结论，但同一 Flow 的新轮次要等资源收尾；不在本次引入跨轮次多份执行绑定。
- **旧 ARCHIVE 作业。** 未派发的旧作业退役；已派发、可能开始删除的作业保留原冻结身份及恢复路径，作为唯一的该次清理拥有者，不能同时再派一项停止清理。晚到真实 ArchiveResult 只用于原执行结清，不重新改变归档业务结论。旧已冻结 effects 与新通知按同一标记核对，不能重复评论；不能为了清除积压删除旧回执或改写其 digest。

正常新合并复用既有 stop / cleanup 能力，无需新增 wire 类型；历史 ARCHIVE 类型保留必要的读取、回执和恢复支持。本方案不承诺在历史归属证据丢失时自动找回目录；业务归档仍须正常完成。

#### 历史积压、实施顺序与运维限制

1. 审批后先在现有 merge / archive 测试中复现“已合并、绑定已释放、仍 blocked”，以及“执行未停但业务应归档”，再实现独立业务提交和 Control 通知收件。
2. 接入已有停止回收、结果应用、inventory 结清与资源释放，覆盖已归档终态及旧 ARCHIVE 的恢复窗口；保持未合并结束路径的既有前提。
3. 新增有界已合并积压恢复。当前 PR reconciliation 明确排除 merged，不能指望旧数据自然重新经过 `record_merge`。只处理当前轮次完整可信的 merge 事实，事务内幂等重检；事实不全或冲突时保留诊断，不补造 SHA、时间或回执。按用户给出的数据，Flow 4 应恢复为成功归档，Flow 3 不变。
4. 调整 CLI / console 的派生说明与通知重试，使“已归档、工作区仍占用”可以如实表达；通知重试不得恢复编码，`on_archived` 改由实际业务归档提交后的既有尽力调用接缝触发。
5. 补升级说明：保存旧持久化 reader，先停写并盘点未决 ARCHIVE / effects，再按最终并发版本基线升级。不能用排空不成功作为自动清理生产目录的理由。新通知收件写入后，旧版本未必能解码，也不了解归档与资源分离；回退需要核对并收敛这些记录，不能直接降级。

本次不执行生产状态更新、Kubernetes 操作、部署或真实 GitHub 写入。未来部署新版本及启动对生产积压的恢复都需要单独授权；不能把本地方案审批当作这类授权。

#### 验证与简化对照（待实施）

使用临时 PostgreSQL、真实临时 Git/jj 目录与现有双端 Session fixture，不访问真实 GitHub 或模型服务。优先扩展现有测试，永久测试只覆盖以下可观察合同：

- 已释放绑定的已合并 Flow 自动归档；重复恢复与重复事件只生成一次 archive 活动与一组通知。PR 仍 open 的执行失败 Flow 不变，旧轮次合并不能结束新一轮。
- 合并时执行仍在运行、Worker 离线或清理失败：业务已归档，原目录仍不可分配；恢复后完成精确清理与 ACK 才能复用。相邻 workspace 的目录、job 和 journal 不受影响。
- 新通知缺少 Worker 也能发布；权限失败或发送后崩溃不退回业务状态。恢复只发送缺项，评论没有虚假的清理声明；Worker 不能提交 Control 通知类型。
- 归档事务在 activity / 通知收件写入处失败时整体回滚；结果、stop 和 ACK 在归档后及 resume 后迟到时，不改写新轮次或新 generation。
- 未派发旧 ARCHIVE、已派发旧清理、旧未决评论与新通知交错时，没有重复清理或评论；未合并的放弃流程仍保持原停止和资源释放前提。

实现后、代码审查前，用同一合同场景做简化对照：移除“资源尚未结清不可复用”保护时，第二个 Flow 会提前取得旧目录；去掉已合并积压扫描时，Flow 4 类旧记录不会恢复；复用 Worker 清理回执作为通知入口时，离线 Worker 场景没有通知。仅在这些对照确实区分正确与错误实现时保留相应机制，不为增加测试数量保留内部调用断言。

最终运行真实 PostgreSQL 下完整 pytest、`uv run ruff check app tests`、`uv run mypy app tests`、console 构建与 `uv build --no-sources`，并核对包内 console。若修改 UI 文案或布局，另行渲染并检查代表状态；纯业务投影变化至少通过 console API / DOM 合同核对。当前只检查计划文档差异和引用，未运行产品测试，未修改产品或测试代码。

#### 尚待落实与不在范围内的工作

审批时需明确接受“业务先归档、同一 Flow 的 resume 仍等旧资源释放”的取舍。实施中先固定并发版本基线与持久化收件版本，再用现有恢复 fixture 核对旧 ARCHIVE 已派发窗口；任何无法证明归属的生产目录继续隔离，不能通过改写计划取消核验。Flow 4 绑定最初释放的原因仍未知，不把此次行为调整当作已经查明该历史原因。跨 Worker 迁移、跨轮次多绑定、自动找回丢失 journal 和生产部署均不在本次实现范围内。

### [002] — 2026-11-11 — 正常资源回收应及时完成

用户在[本次讨论](https://ampcode.com/threads/T-01a1c6c5-ea85-70e7-baf4-76ca5604a43b)补充，旧资源释放在正常情况下预计不应耗时很长。本条补充 [001] 的资源收尾要求，技术方案仍待审批，尚未实施。

无异常时，合并后立即请求原 Worker 停止和回收；结果、ACK 与精确 inventory 到达后直接推进资源释放与重新分配，不增加固定等待，不等待恢复宽限期到期。保留“归档已完成、资源暂不可复用”的状态，只用于真实收尾窗口或 Worker 离线、进程退出未知、清理失败等异常情况；这些等待始终不影响业务归档。

目前没有这条路径的生产耗时测量，不承诺具体秒数。实施验收记录停止、清理、ACK、库存确认和再次可分配的时间点，检查正常路径不依赖恢复定时器触发；异常路径继续验证目录隔离及业务状态不回退。
