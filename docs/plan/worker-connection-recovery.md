# Worker 重连、租约恢复与运维强化计划

> 状态：已批准，实施中 · 日期：2026-08-31 · Feature 3/3
>
> 依赖 Feature 1（远程单 Worker 闭环，PR #6）和 Feature 2（多 Worker 调度，PR #7），均已合并。
> 本 feature 只强化已经工作的多 worker 架构，不改变 task/result 的业务职责和调度公平性。

## 1. 背景与目标

Feature 2 支持多个 worker 正常并行，但沿用保守的 fail-closed 断线策略：started job 在连接丢失后失败，
重复 identity 也不能安全替换旧连接。短暂网络抖动或 `serve` 重启会中断长任务，无法满足常驻微服务的
运维预期。

本 feature 目标：

1. worker 执行中短暂断线后可用同一 identity 和 assignment token 重连，继续 heartbeat 或提交结果；
2. `serve` 重启后从 PostgreSQL 恢复 assignment/lease，多个 worker 自动重连且不重复执行；
3. 新连接取得 connection epoch，旧 socket 的 heartbeat/result 被 fencing；
4. worker 在本地持久记录 assignment 和终态结果，`ResultAccepted` 丢失时可以安全重放；
5. unstarted assignment 可在超时后安全改派，started job 只等待原 worker，到恢复期限才失败；
6. 展示面明确区分 ONLINE/BUSY/RECOVERING/OFFLINE/FAILED，并暴露租约与重连原因。

目标是“同 worker 恢复”，不是把不可迁移的 Codex session/workspace 转给任意其他 worker。

## 2. 范围与非目标

### 2.1 本次包含

- connection epoch、持久 lease/recovery deadline 和 token fencing；
- worker 空闲/忙碌退避重连与同 identity 安全 takeover；
- serve restart 后 registry 重建和 running job 恢复；
- worker root 内的最小持久 journal，保存当前 assignment 与 terminal result；
- result 先持久化、再应用、最终确认的幂等协议；
- lease sweeper 对 unstarted、started、已有 result 三种状态的不同处理；
- RECOVERING 状态展示、结构化日志和确定性 fake-clock 测试；
- 真实进程 smoke：重启 serve、短断 worker connection、重放结果。

### 2.2 本次不包含

- 把 started job 或 attempt 迁移到其他 worker；
- 恢复已经随 worker 进程崩溃而消失的任意 Codex subprocess；
- 多 serve、leader election、共享 WebSocket registry 或控制面 HA；
- exactly-once GitHub/Codex 外部副作用。结果去重缩小窗口，但不能让第三方 API 与本地事务原子化；
- 自动扩缩、worker capability 调度、单进程多槽或 broker；
- 完整 cancel/kill 语义、Web UI 或通用监控平台集成。

## 3. 持久租约模型

### 3.1 字段与状态

扩展 Feature 2 的 worker/job 元数据：

`workers`：

- `connection_epoch`：serve 每次接受注册时生成的随机 epoch；
- `connected_at`、`last_seen_at`、`last_disconnect_reason`；
- 当前 job 与最后恢复时间。

`jobs`：

- `lease_expires_at`：正常 heartbeat 必须在此前续租；
- `recovery_deadline`：started worker 失联后允许同 identity 恢复的最终期限；
- `result_payload`、`result_received_at`、`result_applied_at`；
- 已有 `assigned_worker_id`、`assignment_token` 和 `started_at` 继续作为 affinity/fencing 条件。

ONLINE/BUSY/RECOVERING 是控制/展示状态，不增加互相矛盾的 job 真相：queued/running/done/failed 仍是
持久状态，是否 recovering 由 running job、registry absence 和 recovery deadline 推导。

### 3.2 时间约束

增加 role settings：heartbeat interval、lease duration、recovery grace、reconnect min/max backoff。配置
验证要求 lease 明显大于 heartbeat，recovery grace 大于 lease，并对 backoff 加 jitter。测试注入 clock，
不真实等待这些间隔。

所有时间判断使用数据库/统一 UTC 时间；worker 上报的本机时间只作日志，不能决定 lease 是否有效，
避免多主机时钟漂移改变调度正确性。

### 3.3 Fencing 条件

每个 Started、Heartbeat、Result 和 ResultAck 同时匹配：

```text
worker_id + connection_epoch + job_id + assignment_token
```

同 identity 新连接恢复成功后，Store 原子更新 connection epoch。旧连接从此即使仍能发送缓存消息，也因
受影响行数为零而失效。内存 registry 负责及时关闭旧 socket，数据库条件负责最终正确性。

## 4. 重连协议

### 4.1 注册与恢复声明

worker 的 `Register` 增加可选 recovery descriptor：当前 job id、assignment token、本地 journal state
和 terminal result digest。serve 返回：

- `Registered`：没有当前任务，正常进入 idle；
- `ResumeAssignment`：同 identity/token 的 running job 仍在 recovery deadline 内；
- `ReplayResult`：serve 需要 worker 重发已完成结果；
- `ResultAccepted`：数据库已经完成该 result，worker 可删除 journal；
- `RecoveryRejected(reason)`：token 失效、job 已失败/改派或 deadline 已过，worker 不得继续写回。

新连接只有完成上述协商后才进入 registry 的 ONLINE/BUSY 集合，不会在恢复未决时收到另一个 job。

### 4.2 连接丢失

- 空闲 worker 使用带 jitter 的指数退避重连；
- 忙碌 worker 的 WebSocket 读写与 executor task 解耦，断线不自动取消 executor；
- 同一进程保留 executor，并在后台重连；连接恢复后先续租或提交 terminal result；
- serve 把失联的 started job 标为 RECOVERING 展示态，affinity worker 之外的节点不能领取；
- 超过 recovery deadline 仍未恢复时，job/flow 转 FAILED，任何迟到结果都被 fencing；
- assignment 尚未 Started 时只等 lease expiry，随后清除 token 并交回多 worker scheduler。

### 4.3 Serve 重启

serve shutdown 停止新 assignment，但不把 started job 立即失败；关闭 socket 后 worker 进入重连。新 serve
启动时：

1. 读取 running jobs 和 worker metadata；
2. 不在启动瞬间 requeue started job；
3. 启动 gateway 与 sweeper，等待原 identity 在 recovery deadline 内注册；
4. 对匹配 recovery descriptor 原子换发 connection epoch；
5. deadline 到期后再按 started/unstarted 规则收敛。

Feature 3 仍只有一个 serve owner；“重启同一个控制面”不等于两个 serve 并行接管。

## 5. Worker Journal 与结果确认

### 5.1 本地 journal

每个 worker root 保存一个版本化、原子替换的 current-assignment journal：

- 收到 Assignment 并校验后，先持久化 RECEIVED，再发送 Started；
- executor 开始后写 STARTED；
- 得到 terminal result 后先写完整 result + digest，再通过 WebSocket 发送；
- 只有收到 ResultAccepted 才删除 journal。

journal 不保存长期 GitHub secret；task 中的短时 credential 必须过滤或单独受限存储并在任务结束清除。
root 的权限和独占锁沿用 Feature 2。同 root 第二进程不能并发恢复同一 journal。

进程重启时：

- journal 有 terminal result：允许重连并重放；
- journal 只有 RECEIVED、尚未 Started：报告放弃，让 serve 安全 requeue；
- journal 是 STARTED 但 executor 已不存在：报告不可恢复，serve 标 FAILED，不猜测重跑。

### 5.2 Serve 结果 inbox

serve 收到 Succeeded/Failed 后先在 assignment token 条件下持久化 `result_payload` 和 digest，再由
`JobCoordinator` 应用领域状态/GitHub 副作用，最后写 `result_applied_at`、DONE/FAILED 并返回
ResultAccepted。

重复相同 digest 的 result：

- 已 applied：直接重发 ResultAccepted，不重复执行 coordinator；
- 已 received 未 applied：复用数据库 payload，恢复 coordinator；
- digest 不同：视为协议冲突并拒绝，不能覆盖已接收结果。

该 inbox 防止 worker 因 ack 丢失而再次执行任务。若 serve 在外部 GitHub 副作用与
`result_applied_at` 之间崩溃，仍可能重复第三方调用；现有 adapter 需要后续幂等键/查询补偿，本文不把
它描述为 exactly-once。

## 6. Lease Sweeper 与展示

sweeper 使用 Store 的条件更新批量处理：

- unstarted + lease expired：清除 assignment 并 requeue；
- started + worker offline + deadline 未到：保持 running，展示 RECOVERING；
- started + deadline expired：标记 job/flow FAILED，记录 worker lost；
- result received 未 applied：唤醒 coordinator recovery，不重新运行 worker task；
- DONE/FAILED：拒绝旧 lease 更新并清理 worker current job 展示。

`flowtool workers` 增加 epoch、RECOVERING、lease/recovery remaining 和 disconnect reason；
`flowtool flow show` 显示等待哪个 worker、是否已有 terminal result。日志包含 worker/job/flow/epoch 的结构化字段，
但绝不包含 token、credential 或完整敏感 payload。

## 7. 实施顺序

1. **租约 schema 与 fake clock，测试先行**
   - 覆盖 heartbeat 续租、epoch fencing、unstarted expiry、started grace 和 deadline failure；
   - 实现字段、配置验证和事务 Store API。
2. **恢复握手，测试先行**
   - 覆盖 idle reconnect、busy resume、旧 epoch 拒绝、deadline 拒绝和重复 identity takeover；
   - 扩展 protocol/gateway/client，但保持已有正常调度消息兼容。
3. **Worker journal，测试先行**
   - 覆盖原子状态推进、terminal replay、STARTED 进程丢失和 root lock；
   - 实现最小 journal，不引入通用消息 broker。
4. **结果 inbox 与 coordinator recovery，测试先行**
   - 覆盖 ack 丢失、重复同 digest、冲突 digest 和 received-not-applied 恢复；
   - 实现持久 result pipeline 与幂等确认。
5. **Serve restart / 多 worker 集成**
   - 两个 worker 中一个 BUSY 时重启 serve，证明原 job resume、另一个重新注册且无重复 assignment；
   - 覆盖 grace 到期失败，其他 worker/flow 不受影响。
6. **运维展示与文档**
   - 更新 CLI、CI fault smoke、README、env example、架构/流程/风险文档和本计划结果。

## 8. 验收标准

- 忙碌 worker 短暂断线后以同 identity/token 恢复，不重新执行 job；
- serve 重启后多个 worker 自动重连，started job 仍归原 worker；
- 新 epoch 接管后旧连接所有写入均被拒绝；
- unstarted assignment 到期可安全改派，started assignment 在 grace 内绝不跨 worker 重放；
- terminal result 在 worker/serve 任一侧丢 ack 后可重放，并只应用一次内部 coordinator；
- worker 进程重启后可重放已完成结果，但不会猜测恢复已消失的 executor；
- grace 到期准确失败并可从 CLI 看见原因；
- 所有多 worker 正常路径与 session affinity 测试继续通过。

验证继续包含 pytest、Ruff、Mypy、lock check、build、CLI help、`git diff --check` 和代码 review gate。
CI fault smoke 使用短测试间隔，真实重启 serve/断开 worker socket，不访问真实 GitHub/Codex/bub。

## 9. 风险与回退

- **状态空间扩大**：连接、lease、journal、result inbox 可能组合；用显式状态转换和 fake-clock 表驱动
  测试控制，不以 sleep 猜时序。
- **外部副作用不原子**：result inbox 只能去重 worker 执行和内部应用，GitHub/Codex 的 exactly-once
  仍需 adapter 级幂等设计。
- **worker 永久丢失**：session/workspace 未共享，deadline 后只能失败；跨 worker 迁移另立 feature。
- **本地 journal 损坏**：启动时校验版本/digest 并 fail closed，不删除无法解释的文件，保留人工诊断。
- **控制面单点**：重启恢复不等于 HA；两个 serve 并发仍不受支持。
- **配置误差**：lease/heartbeat/grace 的非法组合在启动时拒绝，防止运行后才暴露不可能恢复的窗口。

回退本 feature 恢复 Feature 2 的断线即失败语义。新增 lease/result 字段可保留但不使用；已经 DONE 的
job 和历史 result 不删除，不执行破坏性数据回退。
