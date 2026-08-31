# 多 Worker 调度与单机多实例计划

> 状态：已实施并验证 · 计划日期：2026-08-31 · 实施/验证日期：2026-09-01 · Feature 2/3
>
> 依赖 Feature 1（远程单 Worker 执行闭环，PR #6，已于 2026-09-01 合并）。本 feature 在其最终提交上
> 实施并保持增量可审查。

## 1. 背景与目标

Feature 1 建立 `serve` 与一个远程 worker 的完整协议，但 serve 会拒绝第二个 identity，任务仍串行。
本 feature 在不改变 task/result 业务契约的前提下，把单连接 gateway 扩展为多 worker registry 和调度器。

目标：

1. 一个 `serve` 同时管理多个经过独立认证的 worker；
2. 不同 flow 可被不同 worker 并行执行，同一 job 永远只派给一个 worker；
3. 同一 attempt 的 Codex session/workspace 固定到首次开始它的 worker；
4. worker 可分布在不同主机，也可作为多个进程/容器运行在同一主机；
5. 每个 worker 首版只有一个执行槽，通过增加实例扩展并行度；
6. admin CLI 可展示每个 worker 的在线/忙碌状态、当前 job/flow 和最后心跳。

Feature 2 完成后支持“多 worker 正常运行”，但仍沿用 Feature 1 的 fail-closed 断线语义。忙碌重连、
`serve` 重启恢复和持久 lease expiry 属于 Feature 3。

## 2. 范围与非目标

### 2.1 本次包含

- 多 worker token 配置、独立 identity 鉴权和并发 WebSocket session registry；
- 单槽 worker 的背压与同机独立 worker root；
- queued job 到空闲 worker 的公平调度；
- INITIAL 首次分配与 attempt affinity 固定；
- 后续 TRIAGE/REVISION/ANSWER/ARCHIVE 只回到 affinity worker；
- PostgreSQL 中可查询的 worker 元数据和 job/flow 关联；
- assignment token 加 worker identity 的 fencing，防止串线和重复完成；
- 两个及以上 worker 的并发、隔离、离线和公平性测试；
- `flowtool workers` 展示命令与同机双 worker CI smoke。

### 2.2 本次不包含

- 单进程多槽、worker capability/标签、优先级队列或资源感知调度；
- 将已开始的 attempt 迁移到其他 worker；
- 忙碌断线重连、结果重放、connection epoch、lease sweeper 或 serve restart 恢复；
- 多 serve、共享 registry ownership、broker 或控制面 HA；
- 自动扩缩容、容器编排器、CPU/内存调度或 worker 远程安装管理；
- Web/Kanban UI。展示面仍是 admin CLI 和内部查询服务边界。

## 3. Registry、身份与单机部署

### 3.1 多连接 registry

Feature 1 的单 worker gateway 拆为：

```text
WorkerGateway
  -> WorkerRegistry[worker_id -> WorkerSession]
  -> JobScheduler
```

`WorkerSession` 保存当前 WebSocket、在线/忙碌状态、当前 assignment 和最近心跳。registry 的可变连接
对象只存在于单个 serve 进程；可展示元数据同步到 Store。

同一个 `worker_id` 同时只允许一条连接。Feature 2 遇到重复 identity 时拒绝新连接，不主动踢掉旧连接；
安全的 epoch takeover 留给 Feature 3。不同 worker 使用独立 token，某个 token 泄漏不能冒充其他 id。

serve 配置由 Feature 1 的单 identity 改为：

```text
FLOWTOOL_WORKER_TOKENS={"worker-a":"secret-a","worker-b":"secret-b"}
```

明文 secret 只在运行配置中，数据库仅保存 worker id 和非敏感元数据。配置解析拒绝空 id、空 token 和
重复值，鉴权继续使用常量时间比较。

### 3.2 单机多 worker

多个 worker 进程/容器可共用同一 `FLOWTOOL_SERVE_WS_URL`，无需监听各自端口。每个实例必须配置：

- 唯一 `FLOWTOOL_WORKER_ID` 和对应 token；
- 独立 `FLOWTOOL_WORKER_ROOT`；默认按 worker id 派生；
- 独立 root 内的 repo/workspace、agent session state、临时文件和生命周期锁。

worker runtime 在连接 serve 前取得 root 独占锁，并写入 worker id 元数据。第二个进程无法取得同一 root
时 fail fast，避免同机 session/workspace 交叉。FlowTool 不自建资源配额；systemd 或容器运行时负责
CPU、内存、磁盘和网络限制。

## 4. 调度与 Attempt Affinity

### 4.1 Eligible job

每个空闲 worker 请求一次调度，scheduler 在事务中选择最早的 eligible queued job：

- 没有 affinity 的 INITIAL 可由任意空闲 worker 领取；
- 已有 `attempt.worker_id` 的 job 只有 identity 匹配的 worker eligible；
- CANCELLED/ARCHIVED/FAILED flow 的 job 不派发；
- 一个 worker 有未确认的 assignment 时不再调度；
- 一个 job 有有效 assignment token 时不能被第二个 worker 领取。

Store 使用行锁与 `SKIP LOCKED` 防止多个 session 并发选择同一个 job。assignment 写入
`assigned_worker_id + assignment_token` 后提交，再通过对应 WebSocket 发送。

### 4.2 首次绑定

控制面为 INITIAL 创建或复用尚未开始的 attempt，但 assignment 前不固定 worker。worker 返回
`Started` 后，在同一 token 条件下原子写入 `attempt.worker_id`：

- started 前连接丢失可以解除 assignment 并改派给其他 worker；
- started 后 affinity 固定，后续任务只等待原 worker；
- 原 worker 离线不会让该 flow 被其他 worker 误执行，也不会阻塞无关 flow；
- 显式 retry 是否创建新 attempt 继续保持当前业务语义，不在本 feature 偷做 session 迁移。

### 4.3 公平性

queued job 按创建时间优先；多个空闲 worker 同时可用时，以 `last_assigned_at` 最早的 worker 优先，id
作为稳定 tie-breaker。目标是避免固定偏向最早连接者，而不是引入可配置权重或复杂资源调度。

scheduler 由三类事件唤醒：新 job 入队、worker 变为空闲、worker 新连接；同时保留短周期数据库扫描，
覆盖 admin CLI 或未来其他进程直接入队后没有内存通知的情况。PostgreSQL jobs 仍是 durable truth。

## 5. 持久化与展示

新增 `workers` 表：

- `worker_id` 主键；
- protocol version、first/last connected、last seen、last assigned 时间；
- 最近状态/错误和 current job id；
- 不保存 token、WebSocket 对象或本地路径中的敏感内容。

Feature 1 已有的 `attempts.worker_id` 成为实际 affinity；jobs 的 `assigned_worker_id` 和 token 更新全部要求
identity/token 同时匹配。worker disconnect 时 Store 更新 OFFLINE/最后错误；在线状态的最终事实仍由
当前 registry 决定，持久值用于重启后的“最后已知”展示。

`ControlService.list_workers()` 聚合 worker、job、flow 和 attempt，供 `flowtool workers` 输出：

```text
WORKER     STATUS   JOB   FLOW   LAST_SEEN
worker-a   BUSY     42    17     2s
worker-b   ONLINE   -     -      1s
worker-c   OFFLINE  -     -      4m
```

该命令是 admin 展示层，不新增未经认证的公网 worker 管理 API。`/healthz` 仍只表示 serve 存活。

## 6. 并发与失败语义

- 每个 WorkerSession 用局部锁串行 assignment/ack/result 状态转换；
- Store token 条件是跨 session 的最终 fencing，内存锁不是持久正确性的唯一依据；
- worker 返回与 identity/job/token 不匹配的 heartbeat/result 时拒绝并记录，不改变领域状态；
- 一个 worker 断线只处理它当前的 assignment，其他 session 和 flow 继续运行；
- Feature 2 沿用 Feature 1：未 started 断线 requeue，started 后断线 fail closed；
- 同 affinity worker 离线时 job 保持 queued，不自动迁移；admin CLI 明确展示等待的 worker id。

## 7. 实施顺序

1. **多 worker 配置与模型，测试先行**
   - 覆盖 token map 校验、worker 表往返和 list_workers 聚合；
   - 实现 role settings、WorkerRecord 和兼容 schema。
2. **Registry，测试先行**
   - 覆盖不同 identity 并发注册、重复 identity 拒绝和 session 独立关闭；
   - 把单连接 gateway 改为多 session registry。
3. **Scheduler / affinity，测试先行**
   - 两 worker 同时竞争两个 job，证明无重复、FIFO、公平性和单槽背压；
   - 覆盖 started 前改派、started 后固定、原 worker 离线时其他 flow 不受影响；
   - 实现事务化 eligible claim 与事件唤醒。
4. **单机隔离与展示**
   - 两个不同 root 可同时启动，同 root 第二个实例 fail fast；
   - 增加 `flowtool workers` 并验证 ONLINE/BUSY/OFFLINE/current flow。
5. **端到端与文档**
   - 同一 runner 启动 serve 和两个 fake-executor worker，并行完成两个 flow；
   - 更新 CI、README、env example、架构/流程/风险文档及本计划实施结果。

## 8. 验收标准

- 一个 serve 可同时保持至少两个不同 identity 的 worker 连接；
- 两个 queued flow 可被两个 worker 分别执行，数据库中 job 不重复领取；
- 一个 worker 同时最多一个 job；完成确认前不会收到下一 assignment；
- 同一 attempt 的所有后续 task 只发给首次 started 的 worker；
- affinity worker 离线不导致其他 worker 接管，也不影响其他 flow；
- 同机不同 root 的 workspace/session 无交叉，同 root 误配置 fail fast；
- `flowtool workers` 准确显示多个 worker 及当前 job/flow；
- 现有单 worker协议和状态机测试继续通过。

验证命令沿用 Feature 1：pytest、Ruff、Mypy、lock check、build、CLI help 和 `git diff --check`。CI smoke
必须在同一 runner 上启动两个 identity/root，并验证终止一个后另一个仍为 ONLINE。

## 9. 风险与回退

- **调度竞争**：内存 registry 与数据库 claim 可能交错；以事务 token 为权威，并用并发测试覆盖。
- **session affinity**：worker 永久丢失时该 attempt 无法迁移；这是显式边界，Feature 3 也只恢复同 id，
  真正跨 worker 迁移需要可恢复 artifact 的独立计划。
- **单机资源争用**：身份/目录隔离不等于资源隔离；部署必须设置外部配额。
- **控制面单点**：多 worker 不代表多 serve；registry 仍只有一个 owner。
- **后续重连**：Feature 2 拒绝重复 identity，短暂抖动仍可能让 started job 失败；Feature 3 再引入安全
  epoch takeover 和 lease recovery。

回退本 feature 可恢复 Feature 1 的单 worker 限制；workers 表和新增元数据可保留但不使用，不删除历史
flow/job/attempt 数据。

## 10. 实施结果

- `FLOWTOOL_WORKER_TOKENS` 提供独立 identity/token 鉴权，registry 允许不同 identity 并发连接并拒绝重复连接；
- PostgreSQL `workers` 表记录 presence、当前 job、心跳和公平性时间，claim 同时实施单槽、FIFO 与 affinity；
- `Started` 以 job/worker/token fence 原子绑定 `attempt.worker_id`，后续任务仅对该 worker eligible；
- worker 默认 root 按 identity 派生，root lock 同时记录 identity；`flowtool workers` 聚合展示 job/flow；
- 单元测试覆盖 registry、配置、root 隔离与展示，PostgreSQL/ASGI 测试覆盖 claim、fencing 和 affinity；
  CI smoke 在同一 runner 启动两个 worker，并验证终止一个后另一个仍存活。

Feature 2 保留计划中的 fail-closed 边界：Started 后断线不会迁移，忙碌重连和 lease recovery 留给 Feature 3。
