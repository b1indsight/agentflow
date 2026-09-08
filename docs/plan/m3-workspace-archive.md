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
