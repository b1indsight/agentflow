# Worker 推理任务下发 — Feature record

**Stage:** 变更 009–010 已实现；实施与验证见变更 011–012，三项修复及代码清理见变更 013–016
**PR:** [#133](https://github.com/b1indsight/agentflow/pull/133)；当前发布提交、审查及 CI 状态以 PR 为准
**Bookmark:** `feat/worker-inference-dispatch`（计划和实现沿用同一个 PR）

**当前阅读入口：** [变更 001](#001--2026-11-06--通用推理合同与-bub-缺配置时自动下发)
定义通用合同与自动选路；[变更 002](#002--2026-11-06--复用-codexamp-独立会话与本地答案工作区)
确定原生后端、独立会话及答案生成边界；
[变更 003](#003--2026-11-06--可恢复失败允许同次调用直接重试) 取代此前延后重试的限制；
[变更 004](#004--2026-11-06--执行记录中的推理失败通知) 增加重试失败后的详情通知。
初稿和先前变更保留为历史；冲突处以较新的变更为准。
**最新要求见变更 009–010：** 推理进入现有 Job 队列和统一分配入口，优先选择本地 bub 微型 worker，
不符合条件时选其他可推理 worker；本地 bub 单推理槽已确认。变更 008 仅合并 Router/Pool、
有 bub 配置就禁止回退、不持久化推理 Job 的提议被本次取代。
手动部署的普通 worker 同时提供 execute/infer；infer 不要求绑定 repo，使用独立的空 workspace。
bub 是 controller 管理、只提供 infer、只有空 workspace 的 mini worker。
保留 Codex/Amp 非 Git 本地工作区、新会话、只生成答案，以及有界重试和详情执行记录通知。
变更 005–007 保留为旧实现记录；本次重构的证据单独记录，不用旧 CI 代替验收。

## 1. Initial plan

### 动机、基线与目标

用户希望 controller 可以把推理任务下发给 worker，首个用途是 review 意图识别和回复生成。
当前两者已经由一次 `Judge.triage(context)` 完成，返回 revise / wait / answer / ignore；
controller 本地运行 bub，随后校验并应用结果。[S1][S2]

本方案基于 fetch 后的远端 main
[`91c005de`](https://github.com/b1indsight/agentflow/commit/91c005de10a344b77b379fc31b26bbc6d1b50138)。
共享 checkout 尚在较早基线，并有其他任务的 Web 控制台改动及执行生命周期计划；本次不移动、
重写或提交那些内容。实施前需在保留这些改动的前提下，从最新 main 准备本功能的独立工作区。
以下通信设计使用最新 main 已落地的 Gateway / Session / Dispatcher，不能按旧 WorkerClient 实现。[S3]

**成功标准：** 开启 worker 模式后，controller 不启动模型进程；worker 收到冻结上下文，
完成识别或生成答案并回传。只有 controller 能创建 revision job、发送 GitHub 回复和消费反馈。
慢推理不阻塞编码任务的 Started、心跳、停止或结果确认；断线不把未处理批次误标为已完成。

**首版范围与提议默认值：**

- 增加显式 `judge_execution = "control" | "worker"`，默认 `control` 保留现有部署行为。
  选择 `worker` 后不自动回退 controller 本地推理。这是待批准的产品选择。
- 复用现有 bub 0.4.1 无工具 Judge，不同时引入 Codex/Amp 推理后端。
- 新增独立 `worker.infer` 请求，首个强类型任务为 `review`。以后可增加真实任务类型；本次不提供
  任意脚本、任意工具、自由 JSON schema 或通用 agent 执行入口。
- review 发给 Flow 已绑定的 worker。它与编码执行槽独立，可以在编码期间处理一项推理；
  不借用其他 Flow 的 worker，不增加能力注册、跨 worker 调度或推理专用节点。
- controller 的 review 循环仍串行；每个 worker 最多一项推理，无等待队列。
- 不增加 TRIAGE JobKind、assignment lease、推理 journal、持久任务队列或数据库表。
  这保留前序 [control-triage](control-triage.md) 删除独立 triage job 的决定，改变的只是执行位置。[S4]
- 不改 Web UI；不调整显式 Request Changes 直接进入修订的现有规则；不把 GitHub 写权限下放。

### 预期行为与失败处理

例如 reviewer 询问某个重试上限：controller 冻结本批反馈和 PR 快照，调用绑定 worker 的
`worker.infer`；worker 返回 `answer` 和答案。controller 重读 PR head/base、确认批次仍有效，
再按原 batch marker 发送一次回复并消费精确反馈版本。[S2]

| 情况 | 首版行为 |
| --- | --- |
| `revise` | controller 保留置信度门槛、PR 基线核验和原子创建 revision job。 |
| `answer` | controller 保留答案持久化、marker 对账和发送授权；worker 只生成文本。 |
| `wait` / `ignore` | 保持现有批次语义。 |
| 未绑定、worker 离线、推理槽忙、连接关闭、RPC 超时 | 返回可分类的调用失败，批次留在 READY；下轮 sweep 可重试，不改成需要新反馈才能唤醒的 WAITING。 |
| 模型超时、非法输出、缺模型配置等已有 Judge failure | 与本地一致，保守 WAIT；不把基础设施错误文本当作回复。修复配置不会自动重跑已经 WAITING 的批次。 |
| 快照不完整 | 沿用本地预检查，直接 WAIT，无需启动远程模型。 |
| 结果身份错误、超大或 schema 不合法 | 拒绝该推理结果，保留 READY；不接受其业务决定。有效连接上的普通推理错误不终止编码任务。 |
| 结果迟到或重复 | 已取消的等待者不再接收；忽略无对应调用的推理回复，不产生 GitHub 操作。 |
| controller/worker 重启或网络中断 | 未保存的纯推理允许重新计算；不保证模型调用恰好一次。已保存的决定由 controller 原恢复路径继续应用。 |

特别注意：**已持久化的答案不能因 worker 离线而失去恢复能力。** worker 可用性检查放在实际
调用 `WorkerJudge.triage` 时；coordinator 使用 `batch.decision` 时不要求任何 worker 在线。

### 技术设计与模块职责

#### 共享 Judge 与配置

将 `app/controler/review/judge.py`、`runner.py` 移到 `app/inference/judge.py`、`runner.py`，
无旧路径 shim。两种执行位置共用同一实现，父进程不 import bub；只有隔离子进程加载模型库。
既有 `app.domain.ports.Judge` 接口保持不变，不新增第二份端口。[S1][S5]

拟定接口：

```python
def context_failure(context: ReviewContext) -> Failure | None: ...

class BubJudge:
    # 现有构造器和 configuration_failure 保持原签名。
    async def triage(self, context: ReviewContext) -> JudgeDecision: ...
    async def decide(
        self, request: JudgeRequest, *, budget_seconds: float,
    ) -> JudgeDecision: ...
```

`triage` 创建 request 并用本地 timeout 调用 `decide`；远程 handler 直接传入 controller 创建的
request，不能重新生成 session_id。`decide` 使用调用预算与本地 timeout 的较小值，模型运行
预算覆盖启动和交换阶段，随后保留现有有界 kill/drain 清理。保留 strict JSON、无工具、输出上限、
临时 BUB_HOME 和显式子进程环境。[S1]

配置变更：

- `ServerSettings.judge_execution: Literal["control", "worker"] = "control"`。
- `ServerSettings` 保留现有 Judge 配置；worker 模式不实例化本地 BubJudge，也不要求 controller
  配置模型 key。现有 `judge_timeout_seconds` 作为远程请求预算。
- `WorkerSettings` 增加 `judge_model: str = ""`、`judge_api_key: SecretStr | None = None`、
  `judge_api_base: str | None = None`、`judge_timeout_seconds: float = 60.0`；timeout 必须有限且正数。
  使用既有 `FLOWTOOL_` 前缀，不读取当前目录 `.env`。
- key 留在实际执行侧，不放入任务、回复、数据库、journal 或日志。保持 coding agent 对
  `FLOWTOOL_*` 的环境过滤，但明确这不构成同 OS 身份下的强隔离。[S6]

#### Wire contract

以下新类型放在 `app/contracts/protocol.py`，使用现有严格 `ProtocolModel`、`ToolCall`、
`ToolResult`，不另建 envelope。继承字段保留现有 `type`、`message_id`、`reply_to`；
外层 Message 承载 connection_epoch。[S7]

| 类型 | 全部新增字段与含义 |
| --- | --- |
| `ReviewInferenceTask` | `kind: Literal["review"]`：任务种类；`request: JudgeRequest`：现有 version/session_id/context；`budget_seconds: float`：有限正数的执行预算。 |
| `InferenceRequest(ToolCall)` | `method: Literal["worker.infer"]`；`arguments: ReviewInferenceTask`。首版只有真实的一种任务，不预建注册表或空 Union。 |
| `InferenceResult(ToolResult)` | `method: Literal["worker.infer"]`；`decision: JudgeDecision \| None`；`failure: Literal["busy", "internal"] \| None`。decision/failure 恰有一个。使用 failure 避免与现有 validation-error 的 error 对象判别混淆。 |

请求沿用 96,000-byte context 上限；结果序列化上限沿用 Judge 的 64,000 bytes，约束执行前和
接收后均检查，不能只依靠整个 Session 的总缓冲上限。模型 failure 仍在 `JudgeDecision.failure`，
不与传输失败混合。`JudgeDecision.model` 记录执行 worker 的配置；controller 不另配期望模型名，
缺配置 failure 允许保持当前空 model 的行为。[S1][S5]

将方法加入 `_FIXED_METHODS`、`ServerCall` 和 `WorkerResult`，同步有界校验。
协议从 v17 升到 v18，拒绝混合版本；持久编码 journal、结果账本及其版本不变。
Register 仍不得携带推理请求/结果；Heartbeat 混合 payload 时，推理结果不借用编码身份。[S7]

#### Controller 调用与关联

新增 `app/controler/workers/inference.py`，不让 `controler/review` 反向依赖 workers 包。

| 类型 | 全部状态字段 |
| --- | --- |
| `InferenceFailure(Exception)` | `code: Literal["worker_unavailable", "busy", "timeout", "invalid_response", "worker_error"]`：安全、可记录的失败类别。 |
| `InferenceBroker` | `_peers: Gateway[WorkerMessageUnion, ServerMessageUnion]`；`_inflight: set[str]`：当前在等待推理的 worker ID，按 worker 而非 epoch 限制并发。 |
| `WorkerJudge` | `broker: InferenceBroker`；`workers: WorkerStore`；`flow_id: int`；`budget_seconds: float`。每批次创建轻量适配器，绑定查询延后到实际推理。 |

```python
class InferenceBroker:
    def __init__(self, peers: Gateway[WorkerMessageUnion, ServerMessageUnion]) -> None: ...
    async def infer(self, worker_id: str, task: ReviewInferenceTask) -> JudgeDecision: ...

class WorkerJudge:
    def __init__(self, broker: InferenceBroker, *, workers: WorkerStore,
                 flow_id: int, budget_seconds: float) -> None: ...
    async def triage(self, context: ReviewContext) -> JudgeDecision: ...
```

`WorkerJudge.triage` 先做上下文预检查，再通过 `get_flow_execution(flow_id)` 获取未释放绑定，
交给 broker；不存在绑定则抛 `worker_unavailable`。Runtime 按配置传本地或远程 Judge，
coordinator 的 `triage(batch, judge)` 和事务/副作用职责保持原样。Runtime 单独捕获
`InferenceFailure`，只记录 batch/worker/类别，避免每次正常离线输出完整 traceback。[S2][S8]

broker 固定选择一次 Registration，先注册回复接收者再发送；总等待预算为 task budget + 10 秒，
给子进程清理和通信留余量。超时、调用者取消、拒绝和关闭都会释放关联及计数。
回复必须匹配 method/reply_to、所选 Session epoch、Judge session_id 和 context digest。
业务身份/大小检查在 broker 完成等待后执行，不在 Dispatcher callback 中抛异常拖垮连接。

现有 ConversationBroker 已有涉及写锁、取消、EOF 与回复竞争的等待逻辑。[S9]
为避免复制这段重要逻辑，在 `app/communication/requests.py` 提取一个暂态调用 helper：

```python
async def request_reply[Incoming: Message, Outgoing: Message, Reply: ToolResult](
    registration: Registration[Incoming, Outgoing],
    call: ToolCall,
    reply_type: type[Reply],
    message: Outgoing,
    *,
    timeout: float,
) -> tuple[Reply, Incoming]: ...
```

helper 的 `ReplyRejected(ValueError)` 仅表示已授权的 payload 拒绝，无额外字段；关闭用既有
SessionClosed，超时用 TimeoutError。它管理本次调用的 Future/send Task/关联清理；限流、任务
选择、重试和业务校验仍属调用方。迁移 ConversationBroker 后保持原行为，不给 Session 或
Dispatcher 新增业务状态。保留 `session.check_request()`，拒绝 serial driver 等待自己的回复。

#### Worker 执行与生命周期

新增 `app/worker/transport/inference.py`，实现独立 `WorkerInferenceCallbacks`。
WorkerRuntime 装配共享 BubJudge，将 callbacks 与现有 state/business callbacks 一起挂载。

其全部状态为 `session: Session[ServerMessageUnion, WorkerMessageUnion]`、`judge: BubJudge`、
`_call: InferenceRequest | None`、`_task: asyncio.Task[JudgeDecision] | None`。

```python
def __init__(self, *, session: Session[ServerMessageUnion, WorkerMessageUnion],
             judge: BubJudge) -> None: ...
def bind(self, routes: Dispatcher[ServerMessageUnion]) -> None: ...
async def on_infer(self, call: InferenceRequest, message: ServerMessageUnion) -> None: ...
async def _outcome(self) -> InferenceResult: ...
async def _send_result(self, result: InferenceResult) -> None: ...
async def aclose(self) -> None: ...
```

- `on_infer` 检查已完成握手，忙则立即回复 busy；空闲时记录 call、启动唯一推理 Task，
  用 `session.watch("inference", ...)` 观察，然后立即返回。模型执行不占用串行 driver。
- `_outcome` shield 等待本对象拥有的 Task；未预期运行异常转为 internal，只记录异常类型。
  `_send_result` 在 driver 上发送关联结果，发送完成后才释放推理槽。
- `_serve_connection` 的 finally 显式 `await callbacks.aclose()`，取消并 join 推理进程清理，
  完成后才允许重新连接。普通通信关闭保留编码 WorkerExecution，却取消可重复的纯推理。
  推理不修改 ExecutionState、workspace 或编码 session。
- runtime 当前只顺序连接一个 server；依靠该生命周期及 finally 保证重连时不重叠启动模型，
  不另加进程级执行 supervisor。清理不得因二次取消而被跳过。[S10]
- Session 的最多四个 watch 需与现有 terminal/tool/stop 路径一起验证，不能为此提高通用上限。

controller 的 `ServerCallbacks` 需要把 `InferenceResult` 与 history 结果一样分类：
迟到回复可丢弃、不要求编码 Started、不刷新编码 receive deadline；有效 epoch 下的 malformed
推理回复只拒绝对应调用。整条混合消息仍先完成 header/epoch/授权校验，不能接受一半 payload。[S8]

### 方案比较、成本与风险

| 选项 | 选择理由或代价 |
| --- | --- |
| 保持全本地 | 改动最小，但不满足 worker 下发需求。作为显式默认模式保留，不能作为远程失败 fallback。 |
| 恢复持久 triage job/lease/journal | 纯推理没有仓库/GitHub 副作用，批次已可恢复；新增一套执行状态成本不必要。 |
| 将 review 当作普通编码 Assignment | 会耦合 workspace、凭据和单编码槽，也可能阻止正在编码的 worker 回答；不采用。 |
| `worker.infer` 的单种强类型任务 | 首版较小，复用现有路由；后续新增真实 kind 时再扩展判别联合。 |
| 复制 history 的 RPC 等待代码 | 会复制取消/写锁/关闭竞争处理；采用小的共享 helper，并对 history 做回归。 |
| 直接用已有 Codex/Amp coding adapter | 它具有 workspace、原生 skills/MCP 等执行能力；本次先保持 Judge 的无工具语义，后续单独设计。 |

worker 模式依赖绑定 worker 在线，配置成本增加，断线可能重复付出模型费用。首版没有跨 worker
转派和持久指数退避，持续离线仍由既有 sweep 尝试；日志只记录分类。串行 review 循环也仍会受
慢 worker 的有界等待影响。本功能不承诺扩容吞吐或模型调用恰好一次。

持有 full-access coding agent 的 worker 不能被宣称为模型密钥的安全隔离域。建议使用 worker
专用限额 key；强隔离需要专用 OS 身份或其他部署边界，不靠环境过滤实现。[S6]

### 实施顺序、部署与回退

1. 获批后从最新 main 建立独立 jj 工作区，保留当前 checkout 其他任务的改动。
2. 提取共享临时请求 helper，迁移 history 调用并回归其并发/取消语义。
3. 移动共享 Judge，增加接收既有 JudgeRequest 的入口；先保持本地行为。
4. 增加 v18 强类型协议、broker、worker callbacks 和 runtime 配置接线。
5. 更新 server 对推理回复的授权分类；验证与编码/停止/恢复共存。
6. 更新 README、架构边界、部署 env 示例与本记录，再运行整体验证和消融。

serve/worker 必须成套升级到相同协议；不修改已有 journal 格式。
先以默认 control 模式验证升级，再配置 worker 模型 key、切换 worker 模式。
仅回退执行位置可设置 control 并准备本地模型配置；二进制回退需两端一起回退。
本阶段只准备本地计划；发布 bookmark、创建 draft PR 等共享操作待明确授权。

### 验证与消融计划（尚未执行）

保留测试以公开行为和真实回归风险为单位，不为每个简单包装新增单测：

1. **真实协议与模型边界：** 经双端 Gateway/Session、实际 bub 库和本机假模型端点完成一次
   远程 answer；controller 无模型 key/本地调用，模型只收到冻结上下文和空 tools，wire 无 key。
   复用现有四类 verdict 的 coordinator 用例，覆盖 revise 才创建 job、answer 才发评论。
2. **暂态失败和恢复：** 断线/忙/超时保持 READY；重连后重算并只应用一次；已保存答案在 worker
   离线时仍能恢复；模型 failure 保持现有 WAIT。控制时间和 I/O，避免真实 provider 调用。
3. **并发与生命周期：** 推理挂起时照常处理 Started、心跳、history、stop/result acceptance；
   第二项推理 busy；旧连接清理完成后才重连；取消和 shutdown 不留下模型子进程。
4. **授权与过期：** 错 session_id/digest、错 epoch、超大/非法回复、迟到结果与混合批次均不能
   应用决定；取消、合并、head/base 漂移以及新反馈版本仍受原 coordinator 检查保护。
5. **回归：** history 等待 helper 的发送锁/取消/EOF 竞争；协议、payload validation、包边界、
   Judge、本地 control triage、review batches 和远程执行恢复。worker 主进程不加载 bub/control/DB。

实施验证使用项目现有 PostgreSQL 测试环境，运行相关 pytest 后再运行全套 `uv run pytest`、
`uv run ruff check app tests`、`uv run mypy app tests`，按 CI 构建 console 与 `uv build --no-sources`。
真实 GitHub/provider smoke 如未执行必须单列，不把替身测试写成真实环境验收。

消融在实现完成后、自动审查前使用临时改动运行，恢复后复验，不永久保留测试开关：

| 暂时移除/简化 | 对照与判断 |
| --- | --- |
| request/session/context 身份检查 | 错配与旧结果场景应能暴露错误决定被接收，证明不可删除。 |
| 后台推理改为 driver 内直接 await | 受控慢模型期间的 stop/Started 应被阻塞，说明独立 Task 必要。 |
| 将 RPC 失败转换成语义 WAIT | worker 重连后批次不能自动继续，证明需要区分两类失败。 |
| 断线清理与重连 join | 原子进程仍存活或出现并行模型调用，证明清理所有权必要。 |

### 待批准项、实施问题与依据

批准本方案意味着接受：**默认保留 control，显式开启 worker；首版仍用 bub；只选 Flow 绑定
worker；纯推理可重算，GitHub 写入保留在 controller。** 若希望默认全量迁移，或希望 worker
直接使用现有 Codex/Amp，应先改这些设计，再开始实现。

实施时核对具体测试夹具、打包入口和部署示例；这些是接线问题，不引入另一套任务系统。
原生 agent 推理、跨 worker 调度、独立推理集群、并行 review 批次、持久退避及 UI 记录均后置。

本轮仅核查源码和草拟设计。方案咨询由本地 Claude Code 完成，初始化与响应模型均为
`claude-opus-5-5`，tools/MCP 均为空，进程成功退出；其建议已对照源码筛选。
没有运行功能测试，也未声称本功能已经实现。

### 源码依据

- [S1 — Judge 与隔离子进程](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/controler/review/judge.py#L22-L143)
  及 [无工具 runner](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/controler/review/runner.py#L24-L60)。
- [S2 — 批次判断、答案应用与恢复](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/controler/workflow/coordinator.py#L91-L216)。
- [S3 — Gateway、回调与单一 driver](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/communication/gateway.py#L39-L96)。
- [S4 — 删除独立 triage job 的既有决定](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/docs/plan/control-triage.md#L205-L239)。
- [S5 — Judge 模型](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/domain/judge_models.py#L11-L47)
  与 [已有 Judge 端口](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/domain/ports.py#L37-L40)。
- [S6 — coding agent 环境与隔离限制](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/worker/execution/credentials.py#L8-L21)。
- [S7 — 当前协议与请求/回复联合](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/contracts/protocol.py#L448-L628)。
- [S8 — server 回复授权分类](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/controler/workers/callbacks.py#L285-L344)
  及 [无 job 的待判断批次](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/persistence/reviews.py#L382-L403)。
- [S9 — 现有暂态 RPC 等待](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/controler/workers/conversation.py#L38-L122)。
- [S10 — worker 顺序重连、装配及关闭](https://github.com/b1indsight/agentflow/blob/91c005de10a344b77b379fc31b26bbc6d1b50138/app/worker/runtime.py#L153-L258)。

## 2. PR change timeline

### 001 — 2026-11-06 — 通用推理合同与 bub 缺配置时自动下发

**状态：** 用户明确要求修改需求；以下技术细节为修订提案，后端范围待确认，未获实施批准。

#### 需求更新与取代范围

用户要求“新增的路由是个足够泛化的功能，可以接受现有的所有推理任务的下发”，并明确
“当 bub 未配置时，下发给当前可以推理的 worker”。

因此取代初稿中的以下选择：

- 删除 `ReviewInferenceTask` 和 wire 上的 `JudgeDecision` 专用合同；路由不认识 review、Flow
  或业务任务枚举。后续增加一种文本推理任务不应再改 worker 协议和路由分支。
- 删除拟增加的 `judge_execution` 开关；由 controller 的 bub 配置情况自动选择执行位置。
- 删除“只选 Flow 绑定 worker”及 `WorkerJudge.flow_id/WorkerStore` 依赖，改为跨 worker 能力选择。
- 不再预设 worker 必须运行 bub；后端选择独立于通用合同，具体范围见本节末尾的待确认项。

本轮 fetch 后基线为
[`52cbbced`](https://github.com/b1indsight/agentflow/commit/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2)，
本功能涉及的推理、通信、协议及 worker 源码与初稿基线没有差异。其他任务的新本地改动仍保留。

#### 通用推理合同

当前扫描到的无仓库副作用模型调用是 review triage，其中一次调用同时完成识别与答案生成；
编码 start/resume 则通过 CodingAgent 修改仓库。[A1][A2]
本提案把“推理任务”解释为输入文本/上下文、返回文本或结构化结果；现有有副作用的
INITIAL/REVISION 继续使用 `worker.execute`，不能因接入新路由而失去 workspace、assignment
及恢复校验。多模态、工具调用和已有编码会话续跑不在本次通用文本推理合同内。

拟新增 `app/domain/inference_models.py`，所有模型 `extra="forbid"`，字段如下：

| 类型 | 字段、类型与用途 |
| --- | --- |
| `InferenceMessage` | `role: Literal["system", "user", "assistant"]`；`content: str`，有界文本。 |
| `InferenceTask` | `version: Literal[1]`；`request_id: str`（32 位 hex，本次调用身份）；`messages: list[InferenceMessage]`；`output_schema: dict[str, JsonValue] \| None`（纯数据）；`max_output_tokens: int`（正数）；`timeout_seconds: float`（有限正数）。 |
| `InferenceOutput` | `request_id: str`；`request_digest: str`（完整规范化 task 的 SHA-256）；`text: str`；`adapter_version: str`（实际执行适配器标识）；`model: str \| None`（后端实际报告，未知不编造）。 |
| `InferenceCapability` | `formats: set[Literal["text", "json"]]`；`max_input_bytes: int`；`max_output_tokens: int`。表示可受理的输入/输出要求，不列 review 等业务名。 |
| `InferenceFailure` | 分类枚举，区分 `unavailable`、`busy`、`timeout`、`invalid_response`、`configuration`、`backend_error`；不携带原始 stderr、密钥或 provider 响应。 |

请求总量拟限 128,000 bytes（覆盖当前 96,000-byte review context、指令与 schema），响应正文
仍有 64,000-byte 硬上限；同时限制消息条数及结构嵌套。controller 与 worker 都验证上限。
JSON schema 只作为输出约定，不执行其中内容或访问外部 `$ref`。真正的业务校验由调用方的
Pydantic 模型负责，不能把“是合法 JSON”当成“业务决定有效”。

wire 保留方法 `worker.infer`：`InferenceRequest.arguments: InferenceTask`，继承的 message_id
使用 request_id；`InferenceResult` 返回 `output: InferenceOutput | None` 或
`failure: InferenceFailure | None`，两者恰有一个。保留 message/reply/epoch 身份关联及晚到结果
丢弃，不传业务模型、执行命令、模型 key 或 GitHub 凭据。

统一端口及调用签名为：

```python
class Inference(Protocol):
    async def infer(self, task: InferenceTask) -> InferenceOutput: ...

class InferenceRouter:
    def __init__(self, *, local: Inference | None, workers: InferencePool) -> None: ...
    async def infer(self, task: InferenceTask) -> InferenceOutput: ...

class InferencePool:
    async def infer(self, task: InferenceTask) -> InferenceOutput: ...

class ReviewJudge:
    def __init__(self, inference: Inference) -> None: ...
    async def triage(self, context: ReviewContext) -> JudgeDecision: ...
```

`InferenceRouter` 只有 `local/workers` 两个依赖。`ReviewJudge` 负责现有 review prompt、冻结
context 的输入组装、`TriageVerdict` 严格解析和 JudgeDecision 构造；`BubInference` 负责通用
无工具模型调用，runner 不再硬编码 review prompt 或导入 TriageVerdict。
worker 处理相同的通用 task，不需要了解它来自 review 还是其他调用方。

`JudgeDecision.adapter_version` 当前固定为 `bub-0.4.1/v1`。[A3]
需要改为有界适配器标识，并由实际结果填写；保留读取已保存旧决定的能力，不能把 Codex/Amp
结果伪记为 bub。既有 context digest、session_id、批次事务和 GitHub 副作用边界仍然保留。

#### 自动选路与能力准入

controller 的路由配置判断与 bub SDK readiness 分开，不能复用目前混合多种原因的
`configuration_failure()` 来决定转派。[A4]

- **未配置：** model/key 均未提供（空白视为未提供），自动调用 worker 池。
- **已配置：** model/key 完整且格式有效，使用本地 BubInference。
- **部分或错误配置：** 只提供 model 或 key、非法 model 或仅设置 API base 时明确诊断；
  提议不悄悄改用 worker。这个细分是实现建议，并非用户已指定的错误策略。
- 已配置后的 SDK 不可用、鉴权失败、429/5xx、超时或非法输出，保留错误，不自动跨执行位置
  重跑。用户要求的触发条件是未配置，而非任何一次模型失败。

Register 增加显式 `inference: InferenceCapability | None` 字段；null 表示此 worker 没有可用
推理入口。沿用 v18 协议升级，不额外兼容 v17 worker。声明与本次认证连接绑定，不写入数据库
成为长期能力事实；声明不是 provider 可达性保证，也不在健康检查里付费调用模型。

InferencePool 持有 peers、WorkerStore、按 worker ID 索引的候选、已预留 ID 集合及轮转顺序；
每个候选仅记录 `session` 和 `capability`。WorkerStore 只用于检查 draining/恢复/失败等状态，
不再查询 Flow binding。每个 worker 一个独立推理槽，不因其编码槽 busy 就排除它。

准入必须同时满足：认证通过、握手已建立、当前 Gateway 仍指向同一 Session/epoch、能力覆盖
请求要求、非 draining/recovering/failed、推理槽可预留。按轮转公平选择，预留在第一次 await
前完成，worker 仍执行最终 busy 检查。Gateway.get 只说明连接已发布，不能独自证明业务就绪。[A5]

候选在 Registered 成功发送且注册流程完成后进入池；连接替换或关闭按 Session 身份撤下，
旧连接的 cleanup 不能删除新连接候选。ServerCallbacks 的注册、恢复及关闭路径都要覆盖，
不只在普通空闲握手里登记。[A6]

没有可用候选或全忙时，review 批次保持 READY，既有 sweep 以后重试；已明确 busy/未受理
可在同一总预算内尝试下一个候选，禁止无限遍历或并行广播。发送后结果未知的跨 worker 重试
策略取决于下方后端副作用边界，不能提前承诺可安全重算。

已经保存的 JudgeDecision 继续先应用，完全不依赖 bub 配置、当前候选或 worker 在线状态。

#### 后端范围：实施前仍需确认

“当前可以推理的 worker”有一个会改变实现的边界：是否包括复用 worker 已配置的 Codex/Amp，
而非要求 worker 另配 bub。用户本轮没有明确选择后端，不把它默认为仅 bub。

现有 Codex/Amp 适配器使用 full-access/native skills/MCP；在临时目录调用 `start`、不给
Flow context，并不能保证其没有工具或外部副作用。[A2][A7]
因此旧稿“无工具、可自动重算”的承诺不能直接套到这些 coding adapters 上。

**建议确认的方向：** 包括已有 Codex/Amp 登录态，但为推理建立独立入口，保持与 Flow 编码
会话、工作区及 push/MCP 凭据分离。实现阶段需验证它能满足的工具权限边界；只有真正满足
所需执行约束的后端才声明能力，不能仅凭安装了 CLI 就宣称可用。
若希望继承现有 agent 工具能力，则还需按可能有副作用的执行设计未知结果恢复，不能沿用
纯计算的自动重试。此处不未经确认增加持久任务系统，也不凭提示词宣称隔离成功。

#### 实施与验证调整

保留初稿的共享暂态 RPC helper、后台推理不阻塞 driver、结果身份校验与 controller 负责
GitHub 写入。实施顺序调整为：确认后端边界 → 通用模型/业务 Judge 拆分 → 本地 bub 通用化
及自动选路 → worker 后端/能力握手 → 跨 worker 池与路由 → 集成、文档及验证。

新增或调整的验收重点：

1. **泛化：** review 识别/答案与一个无关的结构化摘要测试使用完全相同的 route；新增测试任务
   不需改协议、任务枚举或 worker handler。不以源码字符串断言代替运行行为。
2. **自动路由：** bub 缺配置走池；完整配置走本地；半配/错误配置不隐式转派；本地模型报错
   不触发 worker 二次推理。无候选不消费反馈，已保存答案在零 worker 下仍可恢复。
3. **能力和公平：** 排除无能力、要求不匹配、draining 和旧连接；跳过忙推理槽，能选不同 Flow
   的 worker；轮转公平且无并发超额；旧 epoch 关闭不能删除新候选。
4. **业务边界：** 通用层不负责发送 GitHub 回复、修改 Flow 或创建 revision；原四种 verdict、
   PR head/base 漂移、取消和新反馈版本保护全部回归。
5. **后端校准：** 对最终选定的每种后端验证结构化输出、实际工具权限、凭据范围及取消清理，
   根据验证结果确定是否允许未知结果重试。CLI help/模型自述不能作为无副作用证据。

消融继续验证身份校验、异步执行和传输错误分类，新增“取消能力/epoch 准入”及“把半配或
provider 错误一律当作未配置”的对照。只有无副作用后端获确认后，才能保留原稿重算类消融。

**实际交付：** 本轮只更新计划，没有新增实现/测试，未运行功能验证，未提交或推送。
方案修订咨询的初始化和响应均为 `claude-opus-5-5`，tools/MCP 为空，进程成功退出；
咨询建议已按实际源码修正，尤其没有采用旧 worker 兼容或直接复用 full-access adapter 的假设。

#### 本次源码依据

- [A1 — 当前唯一 completion 调用及 review 输出解析](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/review/runner.py#L40-L94)。
- [A2 — CodingAgent 与 Judge 端口](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/domain/ports.py#L13-L39)
  及 [Codex full-access 启动](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/codex.py#L55-L82)。
- [A3 — 已持久化 JudgeDecision 的固定 adapter 标识](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/domain/judge_models.py#L11-L34)。
- [A4 — 当前配置缺失与格式/SDK 检查](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/review/judge.py#L30-L41)。
- [A5 — Gateway.get 的发布而非 readiness 保证](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/communication/gateway.py#L141-L151)。
- [A6 — 普通注册流程](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/workers/callbacks.py#L221-L248)
  及 [恢复注册](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/workers/callbacks.py#L172-L196)。
- [A7 — Amp 原生 MCP/执行接入](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/amp_agent.py#L121-L149)。

### 002 — 2026-11-06 — 复用 Codex/Amp 独立会话与本地答案工作区

**状态：** 用户已明确后端、会话、工作区及答案生成范围；以下实现细节为修订提案，
整体计划尚待批准，未修改实现或测试。

#### 已确定的需求与取代项

用户明确要求：“复用 worker 的 Codex/Amp，使用独立任务会话，使用一个不与实际 github
相关联的本地 workspace 来做这些会话，暂时目标是只生成答案。”

- 关闭变更 001 的后端待确认项。worker 使用其 `coding_agent` 已选定的 Codex 或 Amp，
  不要求另配 bub，不增加第二套推理后端选择配置。现有 runtime 已按此设置选择适配器。[B1]
- 每项推理调用都启动新会话，不 resume，不复用 Flow 的编码会话或 checkout。
- 复用变更 001 的通用 `worker.infer`、自动选路、能力池及独立推理槽；无执行位置开关，
  不限定 Flow 绑定 worker。完整 bub 配置仍在 controller 本地推理。
- “只生成答案”包括 review 意图识别所需的结构化判定与回复文本，不删除已有四类 verdict。
  worker 只返回数据；controller 保留校验、发送回复和创建 revision job 的现有职责。
- 取代初稿“worker 运行无工具 bub”的实现和部署步骤，也取代变更 001 中须先提供无工具后端
  才能接入的前提。本次不新增 OS 沙箱，不把原生编码任务迁到该路由。

#### 原生适配器、工作区和会话所有权

新增小型 `NativeAgentInference`，复用 `CodexAgent` / `AmpCodingAgent.start()`，
不经 `WorkerExecutor` 的仓库准备、VCS 校验、commit/push 或编码任务恢复流程。
传入 `context=None`；请求不能选择命令、可执行文件、后端、cwd 或原生 session ID。[B2]

建议将 `{worker_root}/inference/` 作为本地推理根，每次调用由 worker 随机创建私有空子目录。
不直接拿 request_id 拼路径，不 clone，不创建 remote，不链接真实项目文件；输入全部来自
controller 冻结后的文本。解析路径后检查推理根不属于既有 Git 仓库或 worktree，包括父目录
仓库发现和符号链接指向的仓库；不满足条件时诊断并不声明推理能力。

- 每次只调用 `start`，不传旧 thread ID；原生会话 ID 不写入 Attempt、编码会话历史或
  ExecutionState，也不作为后续推理的恢复入口。[B2]
- `context=None` 不注入 Flow push/GitHub capability；Codex 禁用 Flow MCP，Amp 用不可用
  端点覆盖已保存的 Flow MCP。其他原生配置保持现状。[B3][B4]
- schema 由包装器写入 worker 拥有的临时文件，传给既有 `output_schema` 参数；不接受远程
  文件路径。消息角色有明确边界，业务输入作为数据；答案契约要求仅根据所给内容作答，
  不执行仓库、GitHub 或其他外部写入任务。上下文不足时返回相应判定，不自行 checkout 补全。
- 有效执行预算取请求预算和已配置 agent timeout 的较小值，覆盖启动与模型执行；外层取消
  不修改共享适配器的 timeout。进程清理另留有界余量，不能因第二次取消跳过 join。
- 完成、失败、超时或断线后先终止并回收拥有的进程，再删除该次临时目录。连接关闭的清理
  完成后才重连。崩溃遗留目录不复用；不能仅凭 root lock 就断言遗留子进程已退出。
- 原生 CLI 可能在 workspace 外保存会话记录，删除临时目录不等于删除原生历史；
  本功能不增加历史展示，也不承诺 CLI/provider 侧零留存。[B4]

#### “只生成答案”的权限边界

这是任务范围和返回值契约。独立 cwd 与不注入 Flow 凭据可以避免正常路径绑定真实仓库，
**不能保证原生 agent 没有工具或访问宿主其他文件的权限。** Codex 当前使用 full access；
Amp 当前也运行原生工具。已有环境过滤保留 CLI 登录态、用户配置、其他 MCP 和自定义凭据，
明确不构成账号文件隔离。[B3][B4][B5]

因此不声称 agent 无法访问 GitHub，也不把只给提示词称为安全沙箱。来自 review 的不可信内容
仍有提示注入风险；部署继续遵守既有 trusted-host 和外部账号/容器/VM 隔离要求。[B6]
首版按本段明确的权限边界实施，不额外引入一套工具白名单或权限开关。

worker 只有在既有后端配置有效、可执行文件存在、trusted-host 前提满足且推理根可用时声明
能力；不为健康检查付费调用模型。声明表示本地具备执行条件，不能证明登录、配额或 provider
此刻正常。运行时鉴权、配置和模型错误返回分类失败，不伪装成答案。

#### 通用合同与失败策略的校正

1. **可执行的上限：** 变更 001 的 task/capability `max_output_tokens` 改为
   `max_output_bytes`。两个现有原生入口没有共同的硬 token 上限参数，不能虚报支持。
   保留请求总量 128,000 bytes、答案正文最多 64,000 UTF-8 bytes 的上限；超限失败，不截断成
   成功。输入能力还须覆盖渲染后的 prompt 大小；Codex 当前经 argv 传 prompt，实施时验证
   目标平台的启动限制，必要时缩小其声明上限。[B2][B3]
2. **有界读取：** 最终答案文件按上限加一字节读取；native stdout/stderr 的诊断流单独限长。
   Amp 已有有界并发读取；Codex 目前 `communicate()` 无界，需补齐相同的受控捕获与取消
   清理，再验收推理路径。不能只在读取全部输出后检查长度。[B3][B7]
3. **结构化输出：** Codex 传 schema，Amp 用提示要求 JSON；两者都须经 controller 严格解析。
   capability 的 `json` 表示接受 schema 请求，不表示模型结果必然符合。保留无外部 `$ref`
   获取和业务层校验；worker 不导入 review 判定类型。[B3][B4]
4. **来源信息：** 现有 `CodingExecutionResult` 没有实际模型名，因此 native 输出的 `model`
   为 null，adapter 标识区分 Codex/Amp 和包装器版本。同步允许新 `JudgeDecision.model`
   未知，仍读取已有字符串；不推测模型名，也不把 native 结果标成 bub。[B8]
5. **受理前转派：** 确定未发送或 worker 明确回复 busy/未受理，可以在同一总截止时间内
   尝试另一候选，每个候选至多一次；无候选则返回 unavailable，不并行广播。
6. **发送后结果未知：** 断线或 RPC 等待超时后，本次调用不立刻转派第二个 worker。
   review 批次保留 READY，后续 sweep 以新 request_id 重算；旧结果不得被应用。
   这是基于答案生成任务约定的重试，可能重复推理及费用，不保证模型恰好调用一次，
   也不保证原生 agent 若违反约定产生的外部副作用不会重复。
7. **已返回的失败：** 区分上述传输失败与 worker 明确报告的模型执行超时、鉴权/配置错误、
   无效模型输出。后者沿用既有 Judge failure 的保守 WAIT 处理并诊断，不在同轮转派，
   不新增无限重试或内存退避状态。非法协议结果仍拒绝，不当作有效模型判定。[B9]
8. **保存后恢复：** 已保存且有效的 JudgeDecision 仍由 controller 直接应用，不要求 worker
   在线，也不因 bub 配置变化重新推理。不增加 TRIAGE job、lease、journal 或持久推理队列。[B9]

#### 实施和验收调整

获批后依次完成通用合同与 ReviewJudge 拆分、本地 bub 自动选路、原生推理包装器与工作区、
能力握手及 worker 池、双端通信接线，最后更新部署说明并执行相关及整体检查。
serve/worker 同步升级协议；worker 沿用已有 Codex/Amp 配置和登录，不新增 bub key。

在初稿和变更 001 的验收基础上，补充：

- 对 Codex、Amp 分别经真实 worker 适配器及受控 CLI 替身执行文本/JSON 任务，检查新会话、
  独立 cwd、无 Flow capability、schema 传递、原生模型名未知及输出校验；不能只 mock 包装器。
- 同一 route 完成 review 和非 review 的结构化摘要，不新增业务任务枚举或 worker 分支。
- 重叠运行编码与推理，确认工作区、session、结果互不串用；第二个推理占槽失败不影响编码。
- 检查正常结束、失败、超时、取消、断线、输出溢出时的进程和目录清理；父目录/worktree
  或符号链接关联真实仓库时拒绝准入；对 Codex 输出捕获的调整回归编码 start/resume。
- 验证确定未受理可换 worker、发送后未知不在同轮重复派发、以后 sweep 可重算，以及已保存
  答案在零 worker 时仍可恢复。模型失败 WAIT 与传输失败 READY 分开断言。
- 使用冻结输入和 GitHub 替身核验只有 controller 应用结果；不向真实仓库写入。
  替身测试不能证明原生工具被禁用，真实 provider/CLI smoke 若未执行须单独列明。

**实际交付：** 仅更新本计划；未写实现或测试，未运行功能验证，未提交、推送或部署。
本轮方案咨询的初始化和响应均为 `claude-opus-5-5`，tools/MCP 为空且进程成功退出。
未采用咨询草案中的新增内存退避、所有模型错误自动重试，以及仅凭根锁清理崩溃遗留目录等
建议；它们不属于当前需求，或缺少可靠的生命周期依据。

#### 本次源码依据

- [B1 — worker 按 coding_agent 装配原生适配器](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/runtime.py#L128-L149)。
- [B2 — 现有 start/resume 接口](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/domain/ports.py#L13-L33)。
- [B3 — Codex 环境、启动与进程回收](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/codex.py#L26-L181)。
- [B4 — Amp 独立 start、schema 提示与原生执行](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/amp_agent.py#L102-L157)。
- [B5 — 原生环境继承及隔离限制](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/credentials.py#L8-L20)。
- [B6 — worker 根目录和执行隔离前提](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/config.py#L24-L47)。
- [B7 — Amp 有界流读取和取消清理](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/amp_process.py#L142-L202)。
- [B8 — 编码结果当前没有模型元数据](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/domain/models.py#L251-L258)。
- [B9 — 复用已保存决定及 Judge failure 转 WAIT](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/workflow/coordinator.py#L95-L143)。

### 003 — 2026-11-06 — 可恢复失败允许同次调用直接重试

**状态：** 用户明确要求“可恢复失败允许直接重试”；下述分类及次数为实现提案，尚未实施。
本条取代变更 002 中“发送后结果未知不在同轮重试”和“执行超时一律进入 WAIT”的限制，
同时更新对应验收项。保留原生后端、独立会话、答案生成及 controller 应用结果的职责。

#### 重试分类与归属

controller 的 `InferenceRouter` 统一拥有同次调用的重试循环；pool 负责单次候选选择及调用，
worker、临时 RPC helper 和 Dispatcher 不另加重试。执行位置在调用开始时选定：
bub 完整配置时可以在本地重试，不能因失败改走 worker；缺配置时才使用 worker 池。

失败须由适配器根据可验证的运行事实分类，不读取模型自报的 retryable，也不匹配任意 stderr
来猜测可恢复性。变更 001 的 `InferenceFailure` 枚举改为严格失败对象，字段为
`code`（下表枚举）和 `retry_after_seconds: float | None`（可选有限非负等待秒数）。
等待建议只来自可信的后端元数据，不携带原始 provider 报文或凭据。

| 类别 | 处理 |
| --- | --- |
| `busy`、`connection_closed`、`rpc_timeout` | 同次调用允许换可用 worker 重试，含已发送但结果未知的连接中断。 |
| `execution_timeout` | 由进程监督器或后端明确报告的执行超时；有剩余总预算时允许重试。 |
| `rate_limited`、`backend_unavailable` | 可靠识别的临时限流、临时服务故障；同次调用有界重试，遵守等待建议。 |
| `unavailable` | 无可用 worker 时立即结束本次调用，不空转；未来 sweep 仍可尝试。 |
| `authentication`、`configuration` | 鉴权或配置错误不直接重试。永久额度不足不能仅因 HTTP 429 被归入临时限流。 |
| `invalid_response`、`output_limit`、`backend_error` | 非法输出/身份、超限及无法可靠分类的后端错误，不盲目重试。 |

调用者取消、总截止已到、合法的语义 WAIT 均不触发重试；已保存的答案也不重新生成。
现有 bub runner 将所有 provider 异常压成 `model-error`，需要在脱敏前保留可靠的暂态分类；
SDK 异常类型须在实施时核对。现有原生结果也缺少类型化原因，需保留监督器已知的 timeout/
overflow 事实，不能仅凭退出码 124 推断原因。不透明 CLI 错误保持不可自动重试。[C1][C2]

#### 次数、预算和结果关联

- **建议默认最多 3 次尝试（含首次）**，不是每一层各重试 3 次，也不等于原生 CLI 内部只
  调用 3 次模型。bub SDK 保持 `max_retries=0`；原生 CLI 内部行为不作额外保证。[C1]
- 所有尝试、退避和通信共用一个单调总截止；重试不重置预算。单次执行时间受剩余预算及
  后端单次上限约束，总预算耗尽后不再启动任务。终止及回收进程仍执行，并单独保留有界余量。
- 忙或断线后有其他就绪 worker 时直接转派；对临时限流/服务故障使用有界短退避。有可信
  `retry_after_seconds` 时不得提前重试；若等待后已无执行预算，立即返回重试耗尽。
- 优先选其他可用 worker。仅当原任务已确认结束并释放槽时才允许重用同一 worker；
  对结果未知的原 worker，本次调用不重复投递到其旧连接。
- 每次尝试使用新 request_id、新原生会话和新临时目录，保持业务 messages/schema 不变。
  按该次尝试的完整 task digest、reply_to 和 epoch 校验，只接受当前尝试的结果；旧结果丢弃。
- 不主动并行广播。断线时旧任务可能尚未停止，因此重试仍可能与旧计算重叠、重复费用；
  不承诺模型调用恰好一次，也不把答案生成约定当成原生工具无副作用的证明。

#### 重试耗尽与业务状态

可恢复失败耗尽后返回分类的基础设施失败，review 批次仍保留 READY，之后 sweep 可继续；
不消费反馈，不构造会被 coordinator 转成语义 WAIT 的 JudgeDecision.failure。[B9]
非可恢复失败保留此前的保守处理，非法身份结果仍拒绝。已保存决定的应用和 GitHub marker
对账保持不变；不增加持久计数器、TRIAGE job、lease、journal 或新的后台重试服务。

#### 验收调整（尚未执行）

1. 首次已发送后断线，同次调用换 worker 成功；请求身份、会话和目录不同，迟到旧结果不能
   导致重复应用或重复 GitHub 回复。
2. busy、已确认执行超时、临时 provider 故障可重试；达到次数或总时间上限后停止并保持 READY。
   单次超时发生在总截止之后时不再启动；无候选不忙等。
3. 限流等待建议在预算内时遵守，超过剩余预算时结束；取消发生在执行或退避期间均不再下发。
4. 鉴权/配置、非法 JSON、超限、身份不符、无法分类的 CLI 错误仅尝试一次；合法 WAIT 不重试。
5. bub 暂态错误只在本地重试，worker 不收到任务；worker/SDK/helper 不新增嵌套自动重试。

**实际交付：** 只更新计划及验收要求，未修改实现或测试，未提交、推送或部署。
本轮方案咨询的初始化与响应均为 `claude-opus-5-5`，tools/MCP 为空且进程成功退出；
咨询不代替功能验证。

#### 本次源码依据

- [C1 — bub 禁用 SDK 重试及当前异常压缩](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/review/runner.py#L40-L94)。
- [C2 — Codex 监督器超时](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/codex.py#L169-L181)
  及 [Amp 超时/超限到通用结果的转换](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/worker/execution/amp_agent.py#L68-L91)。

### 004 — 2026-11-06 — 执行记录中的推理失败通知

**状态：** 用户要求“如果 3 次重试失败需要给详情的执行记录一个通知”；本条记录需求和
实现提案，尚未实施。次数沿用变更 003 的 **总计 3 次尝试，含首次**，不改成首次加 3 次。
本条仅为初稿“不改 Web UI”增加这一项例外，其他后端、会话和恢复规则不变。

#### 用户可见行为

- 一轮推理 3 次尝试均未成功后，由 controller 在该 Flow 的“详情 → 执行记录”中显示一条
  持久通知。第 1/2 次失败但之后成功时，不生成“重试耗尽”通知。
- 通知展示发生时间、实际尝试次数/上限、最后失败原因、最后一次的 worker/后端（已知时）
  和后续处理说明。文案使用固定中文模板及安全错误分类，不展示原始 stderr、prompt、密钥、
  工作区路径或模型思考过程，也不把一次推理失败称为整个 Flow 失败。
- 建议对有过尝试、但提前耗尽总预算的失败也通知，明确显示实际次数及“总预算耗尽”；
  不把 1/2 次写成 3 次。零次尝试且无候选、用户取消、合法的语义 WAIT 不伪报三次失败。
- 第 3 次若变为鉴权等不可直接重试的错误，仍应通知“三次尝试均未成功”，但后续处理按真实
  错误及批次状态说明；不得错误承诺自动重试。早于三次终止的非可恢复错误不因此补跑到三次。
- 通知保存在应用内，刷新页面或 controller 重启后仍可读；不发 GitHub 评论、桌面推送或
  临时 toast，不增加通知中心和手动重试按钮。后续成功不删除已发生的失败记录。

#### 接入位置与持久记录

现有执行记录页只展示 `JobSummary`，`/flows/{flow_id}/jobs` 也只查询 jobs。[D1]
推理没有 Job，不能插入虚假的 TRIAGE Job 来显示通知，也不能仅写服务端日志就声称页面可见。

1. `InferenceRouter` 在最终失败时提供类型化的尝试摘要：逻辑调用 ID、实际次数、次数上限、
   停止原因、最后失败分类、最后 worker/后端、结束时间和耗时。它不依赖 UI、ConsoleStore
   或 GitHub。review 调用方在 `app/controler/runtime.py` 的处理边界绑定真实 flow/batch，
   将摘要交给持久层；通用 worker wire 不增加 review/Flow 专用字段。
2. 复用已有 `console_activity` 和 `ActivityStore.append`，使用 `source="flow"`、
   `kind="inference_exhausted"`、`key="inference-exhausted:{batch_id}"`，作者为 controller。
   不增加新数据库表、推理任务 journal 或持久重试队列。[D2]
3. `metadata` 保持现有扁平标量限制，保存 `batch_id`、`logical_call_id`、`attempts`、
   `max_attempts`、`stop_reason`、`last_failure_code`、`last_worker_id`、`backend`、`elapsed_ms`。
   不为通知扩大 Observation 的通用契约，也不塞入每次调用的原始 payload。[D3]
4. 时间和耗时从已经生成的失败摘要取值，补写时不重新取 now。相同摘要重复写入保持相同
   key/digest，复用现有行锁及幂等判断；同一批次后续 sweep 又耗尽时追加同 key 的新版本。
   页面按 key 只显示最新版本，避免每轮扫描新增一张相同批次的通知卡；历史版本仍保留。[D2]
5. 通知写入失败不触发额外模型调用；日志明确记录未完成持久化，不能宣称通知已经送达。
   本次不新增通知 outbox，也不承诺数据库不可用期间的通知恰好一次送达。

#### 读取接口和执行记录页

- 在 `app/contracts/console.py` 增加严格 `InferenceNotice`：`key: str`、`batch_id: int`、
  `occurred_at/updated_at: datetime`、`attempts/max_attempts: int`、`last_failure_code`、
  `last_worker_id: str | None`、`backend: str | None`、`stop_reason`、`elapsed_ms: int`、
  `historical: bool`、`next_condition: str`。错误和停止原因使用已定义枚举，次数/耗时有界。
  `InferenceNoticePage` 为 `items`、`snapshot`、`next_cursor`、`has_more`。
- 在 `app/persistence/console.py` 提供有界通知读取，`app/controler/console/api.py` 增加
  只读 `/flows/{flow_id}/inference-notices`。筛选 source/kind，在固定 activity_version
  快照内取每个 key 的最新版本，再按 `(updated_at, key)` 倒序分页；游标绑定 Flow、通知查询
  类型、快照及排序键。复用 owner-only 访问边界，不读取跨 Flow 数据。
- 在 `app/controler/web/App.tsx` 的执行记录页添加“推理通知”子区，补齐 `types.ts` 和 `api.ts`
  的 DTO/读取方法。保留现有 jobs 接口和分页，不改造为混合 Job 执行系统。
- 通知列表独立分页和错误状态，沿用进入页面/切换 Flow/显式刷新的读取生命周期，并取消或
  丢弃过期请求；不新增轮询或宣称实时推送。一方读取失败不清空另一方的执行记录。[D1]
- 查询实际批次及 Flow 状态生成后续说明。仍为有效 READY 批次时说明后续扫描可继续；
  WAITING 时说明需新反馈等现有触发；已经完成、取消或被取代则标为历史通知。
  历史快照固定事件分页，当前状态说明不声称是事件发生时的状态。
- `source="flow"` 已被会话读取排除，因此通知只进入执行记录，不混入 reviewer/agent 对话。[D4]

#### 恢复规则与验收

通知不改变业务状态：可恢复失败耗尽仍保留 READY；不可直接重试的错误沿用已有处理。
新的有效 PR 评论可按现有规则创建新批次，不把添加通知等同于新增恢复入口。
实施时保留共享 checkout 中其他控制台改动，本次计划更新没有改动那些文件。

以下为新增验收，尚未执行：

1. 经真实推理路由连续失败三次，通过真实 console API 在执行记录看到一条持久通知；
   页面刷新和 controller 重启后仍可读。两次失败后成功不出现耗尽通知。
2. 提前耗尽预算显示真实次数；第三次为不可恢复错误仍显示失败通知和正确后续说明；
   取消和零尝试不伪报三次失败，通知不触发 GitHub 写入或创建 Job。
3. 同一摘要并发/重复写入幂等；后续 sweep 更新同一卡片；不同批次和 Flow 隔离。
   跨页更新按快照稳定读取，游标不能跨 Flow 或跨查询复用。
4. 后续完成/取消后通知显示为历史，WAITING 不显示为自动重试；敏感内容不进入 DTO/页面。
   通知读写失败不清空 jobs、不额外启动模型，不混入会话列表。
5. 回归 jobs 分页、Flow 切换和请求取消；用现有浏览器流程检查桌面/手机的通知、无通知及
   加载失败状态，截图并实际查看。计划更新不等于已完成 UI 或功能验证。

**实际交付：** 仅更新计划，未修改实现或测试，未提交、推送或部署。
本轮咨询初始化与响应均为 `claude-opus-5-5`，tools/MCP 为空且进程成功退出；建议已对照
实际源码复核，尤其保留了 metadata 的标量限制和同 key/digest 的幂等规则。

#### 本次源码依据

- [D1 — 执行记录页及其 Job 列表](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/web/App.tsx#L688-L725)、
  [查询分页](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/persistence/console.py#L370-L406)
  和 [页面刷新及请求取消](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/controler/web/App.tsx#L381-L407)。
- [D2 — 已有活动账本](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/persistence/console_schema.py#L66-L80)
  及 [带行锁及 digest 去重的写入](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/persistence/activity.py#L74-L167)。
- [D3 — Observation 及扁平 metadata](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/github/console.py#L24-L39)。
- [D4 — 会话明确排除内部 Flow 活动](https://github.com/b1indsight/agentflow/blob/52cbbced5fac6e8e67d5d6923a8edc5e96a9cff2/app/persistence/activity.py#L190-L214)。

### 005 — 2026-11-06 — 通用路由、原生会话和持久通知落地

**状态：** 变更 001–004 已实现于独立 `worker-inference` jj 工作区，基于
`main` 的 worker inactivity timeout 改动；共享外层 checkout 的其他控制台工作未改动。
以下实际实现取代初稿接口示例，保留全部历史设计记录。

- `domain/inference_models.py`、`inference/router.py` 和 `domain.ports.Inference` 定义有界
  messages/schema、输出、能力、错误与尝试摘要。wire18 的 `worker.infer` 没有 review/Flow 类型。
  `ReviewJudge` 仅组装上下文和验证业务决定；原专用 runner 已移除，bub 使用通用隔离 runner。
- Runtime 只在 model/key/base 全空时选择 worker；部分配置与本地调用失败均不切换执行位置。
  Pool 在 Registered 后接纳当前连接能力，核对 epoch、状态、能力和独立推理槽，按候选轮转。
  临时请求 helper 与 history 共享写锁、关闭、取消和关联所有权，不加另一层重试。
- Codex/Amp 推理每次 start 新会话，在 worker root 下创建不属于 Git 仓库的临时目录，
  不注入 Flow capability，不经过编码 workspace/VCS 流程。后台任务由连接所有，断线取消并 join。
  输入/输出字节限制、总预算和编码空闲计时分别保留；健康检查不调用付费模型。
- Router 最多三次尝试（含首次），共享单调截止；每次新身份、冻结输入、优先其他 worker，
  未知结果排除原 worker。永久错误不补满次数；取消、零尝试及最终成功不生成耗尽通知。
- 通知使用现有 ActivityStore 的行锁、key/digest 幂等与版本账本，无新增表、Job 或持久重试队列。
  实现放在独立 `persistence/inference_notices.py`，而非继续扩大 console.py：通知写入、快照读取、
  当前状态说明是一个独立且内聚的职责，现有 jobs/会话查询不需要感知它。
- owner-only API 和执行记录的“推理通知”区独立分页、处理读取错误、取消旧请求；无轮询、推理
  重试按钮、GitHub 评论或桌面推送。READY/WAITING/历史通知给出不同的当前处理说明。

**边界：** 新目录仅隔开正常会话/仓库关联，不能禁止原生工具、自定义凭据或用户配置。
分区后重试仍可能与旧计算重叠并重复计费；崩溃遗留进程、CLI/provider 历史不保证清除。
serve/worker 需一起升级 wire18，journal5/inbox1 不变；部署和回退说明已同步。

### 006 — 2026-11-06 — 失败归因、截止与进程清理修正

**触发及实测：** 回归暴露了三个会破坏既定失败规则的边界，均在当前实现中修正。

1. Python `TimeoutError` 是 `OSError` 的子类；bub 先捕获 OSError 会把超时误分类为配置失败。
   调整捕获顺序，真实超时继续按可恢复错误计数；不通过 stderr 猜测错误类型。
2. 清理余量不能扩大答案接受窗口。删除 pool 的额外 RPC 等待余量，router 拒绝总截止之后的
   成功结果；进程取消/join 仍继续完成。晚到答案测试在移除截止检查时失败。
3. 输出溢出会暂停 asyncio pipe transport；仅 kill/wait 可能阻塞，或让清理超时覆盖原失败。
   Codex/Amp 先取消并 join 原有有界读取，再并发丢弃剩余字节与回收进程，清理自身有界且
   屏蔽重复取消。清理超限只记录安全警告，不覆盖原输出超限/取消；不承诺回收任意脱离进程树的 daemon。

另一个回归先复现了“前两次技术失败、第三次 JSON 合法但 review verdict 无效”没有耗尽通知。
现在调用方把严格业务验证作为 controller 内的 `validate` 回调交给 router/pool，在单次尝试
结束之前校验。验证规则仍属 ReviewJudge，worker 和 wire 不认识业务模型；ValueError 归为
不可重试的 invalid_response，并保留实际 worker/后端及三次失败摘要。合法语义 WAIT 不受影响。
保留回调而非引入 JSON Schema 依赖，避免两套业务验证规则漂移。

### 007 — 2026-11-06 — 自动化、真实 API 页面与消融验证

**验收范围：** PostgreSQL 隔离 schema、真实认证/Session/RPC、真实 bub 库与本机假模型端点、
真实 Codex/Amp 适配器与受控 CLI 子进程；所有 GitHub effects 使用替身。未调用真实付费 provider，
未写入真实 GitHub，未部署。原生 CLI 的真实账号登录、额度和工具行为未作线上 smoke 验收。

已运行以下检查（在独立工作区执行；数据库通过 `FLOWTOOL_TEST_DATABASE_URL` 指定）：

- `uv run pytest -q`：全量后端回归；新增路由、通知、原生进程、双端并发/断线以及既有 history/
  review 恢复场景一并验证。自动配置路由覆盖空白、半配、仅 base、错误 model 和完整配置。
- `uv run ruff check app tests`、`uv run mypy app tests`：通过。
- 前端 `npm test`、`npm run format:check`、`npm run build`、`npm run test:e2e`：通过。
  build 包含 TypeScript 检查。保留现有大 chunk 提示和 Python TestClient 弃用提示，未扩展依赖调整范围。
- `uv build --no-sources`：构建成功；wheel 内包含 console 静态资源和通用 runner，无旧 review runner。
  静态资源与当前前端 dist 一致；架构页面 Protocol 卡片的浏览器 DOM 已确认显示 wire18。
- 临时脚本 `.amp/in/verify_notice_ui.py` 从真实 router/runtime 的失败摘要写入 PostgreSQL，
  通过真实认证 HTTP API 和编译后前端检查 READY、第三次鉴权失败的 WAITING、历史和空态。
  历史状态通过隔离数据库夹具构造，自动恢复另由后端用例验证。桌面/手机均检查文案、状态和
  横向溢出并截图；失败读取/保留 jobs 使用 Playwright 拦截响应验证。最终截图均已实际查看。

**消融方式：** 临时 `.amp/in/ablate_inference.py` 在单独 Python 进程中替换一个机制，
使用相同 pytest 输入；不改生产文件、不留永久测试开关。基线通过，以下简化均使相应用例失败：

| 变体 | 实际观察与决定 |
| --- | --- |
| 配置错误一律视作“未配置” | model/key/base 半配和错误 model 被转派；保留配置存在性与有效性分离。 |
| 只尝试一次 | 首次断线后不再换 worker 成功；保留有界重试。 |
| 移除晚到成功截止检查 | 总预算之后的成功被接受；保留总截止校验。 |
| 移除推理槽预留 | 第三个并发调用被发送并超时，替代应有的 unavailable；保留独立槽。 |
| 移除当前 epoch 准入 | 旧 epoch worker 收到新请求；保留连接身份核验。 |
| 移除输出 request/digest 校验 | 错摘要答案被接受；保留身份关联。 |
| 在消息 driver 中 await 推理 | 编码结果无法在慢推理期间被接纳，测试超时；保留后台 task/watch。 |
| 移除通知版本快照条件 | 更新过的旧 key 跨过排序边界，第二页丢记录；保留固定快照。 |
| 技术故障也转语义 WAIT | 批次从预期 READY 变为 WAITING；保留两类失败分离。 |
| 业务验证移出尝试边界 | 第三次非法判定无安全失败摘要，批次错误停在 READY；保留 controller 验证回调。 |
| 移除非仓库目录检查 | Git 父目录/符号链接下错误声明推理能力；保留准入检查。 |

临时验证脚本执行后已删除；消融源码副本与截图保存在[本线程验证附件](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)。
复现时恢复附件中的脚本，执行 `uv run python .amp/in/ablate_inference.py baseline`，再以
`configuration`、`retry`、`deadline`、`slot`、`epoch`、`identity`、`background`、`snapshot`、
`failure-state`、`caller-schema`、`repository-check` 替换 baseline。变体退出 1 表示预期的行为回归；
保留 `.amp/in/ablation-*.log` 和 `native-flood-before.log` 作为本地证据，不纳入产品或 CI。
管道清理采用同一 flooded-child 场景的修复前失败/修复后通过对照，未重复引入阻塞实现。

**交付状态：** 实现、文档和验证已完成，按用户发布指令进入代码审查门禁与 PR 发布流程。
发布前已对齐包含折叠活动续读修复的远端 main；同名分支 PR 与交付记录保存最终发布、审查及 CI 状态。
本记录不代表已合并或部署，也不替代相应授权。

### 008 — 2026-11-06 — bub 改为只提供 infer 的附属 worker（提议）

**需求：** 用户要求降低复杂度，将 bub 抽为附属微型 worker，仅注册 infer 方法。
**状态：** 以下是待确认的替代设计；当前 PR 的实现与测试未改变。本条拟取代变更 005 中
controller 直接调用 bub、Router/Pool 两层组合的结构，保留其余已确定的功能和失败规则。

#### 统一调用路径与明确删除项

- controller 只保留一个推理调用入口；合并现有 Router 与 Pool 的选择、尝试计数和结果校验，
  删除 `InferenceRouter.local`、本地/远程调用分支及只用于连接两层的 `WorkerInference` 接口。
  通用推理请求统一经过 `worker.infer`，不通过给直接函数调用改名来伪装 worker。
- bub 的适配器及短命 SDK runner 归入 worker 侧；附属 worker 与普通 Codex/Amp worker
  复用 `WorkerInferenceCallbacks`。附属 worker 只注册 `worker.infer`，不注册编码、历史或 Flow 工具。
- 保留 `request_reply`：它已经由 history/inference 共用，负责发送锁、关联、断线、超时和清理，
  删除它只会把同一套通信代码复制回业务层。选路之外的重试分类、总截止、业务校验和失败摘要也保留。

#### 附属方式与边界

- 建议默认由 controller 托管一个轻量本地 endpoint，通过有界进程内 Transport 对接两端
  Session/Dispatcher，使用同一请求/回复模型；不新增监听端口、外部服务或部署单元。
  SDK 仍在每次调用独立的 bub 子进程执行，显式环境和临时目录规则不变。
- 不套用完整 `WorkerRuntime`、`WorkerStateCallbacks` 或 `ServerCallbacks`，不建立虚假的
  编码 worker 数据库记录，不运行 assignment/recovery/journal/Flow workspace。
  当前 `ServerCallbacks.poll` 会给空闲连接分配编码任务，不能直接复用它接入 infer-only worker。[B1]
- 本地两端由宿主创建相同的新 epoch；Session 构造器已支持显式 epoch，无须伪造编码恢复握手。[B2]
  两端 driver 就绪后才接受请求；宿主负责关闭、取消和 join，Transport 关闭需唤醒受阻读写。
  本地身份不能被外部连接抢占。异常连接重建前必须完成旧执行清理，重连本身不得重跑推理任务。

#### 维持的行为与本次建议默认值

- bub model/key/base 全空：不挂载 bub worker，选择可推理的普通 worker。
- bub 任一配置存在：只使用附属 bub worker；半配/错误配置按原规则报错，不偷偷回退到其他 worker。
  配置检查可在适配器执行前失败，不能把失败检查说成已经启动了 SDK 子进程。[B3]
- 建议附属 bub 与其他 worker 一样只有一个推理槽，无等待队列；当前 review 扫描本就串行，
  但这会收紧未来并发直接调用 bub 的行为，随本方案一起确认。
- 保持最多三次总尝试及一个总截止、READY/WAITING 区分、既有通知和已保存答案恢复。
  已回传的执行超时与通信结果未知必须继续区分；不能为了重试附属 worker 放松未知结果的排除规则。
- 业务 schema 仍由 ReviewJudge 提供，在统一入口的一次尝试内校验；通知 API/UI 不改，
  编码能力和原生非 Git 独立会话的约束不变。

#### 验收与取舍

1. 同样的通用任务分别经过 bub 和 Codex/Amp 的 `worker.infer`，controller 不再直接调用模型适配器。
2. 空配置、半配、完整配置、槽忙及附属 worker 异常的选路与失败分类有明确结果；不增加隐式回退。
3. infer-only worker 不接编码任务，不生成编码 worker/journal/workspace 记录；外部不能抢占本地身份。
4. 断线、取消、超时与 shutdown 不遗留调用或子进程；晚到结果不改变已结束调用。
5. 复用现有重试、第三次业务输出非法、通知幂等和保存决定恢复测试；既有 history/编码协议回归通过。

不采用完整外部 worker：它会引入额外部署以及与推理无关的编码生命周期。
单纯把 bub 包装为另一个 `Inference` 对象也不满足统一通信路径的目标。
不能预先承诺总行数下降：本地连接管理会增加代码；验收重点是删除重复执行路径和多余调度层，
不再叠加新的通用方法发现框架、持久队列或重试机制。

本次使用 Claude Opus 5.5 只读咨询，已验证初始化与响应模型，tools/MCP 为空且调用成功；
建议经源码复核。咨询建议保留 Router/Pool 两层，本提议进一步合并它们以落实用户的简化目标。
以上均未实施或执行新验收；只更新同一功能记录，未更新远端 PR。

- [B1 — 编码调度入口](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/controler/workers/callbacks.py#L226-L280)
- [B2 — Session 的 epoch 与 Transport 边界](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/communication/session.py#L28-L61)
- [B3 — bub 配置检查与子进程](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/inference/bub.py#L35-L80)

### 009 — 2026-11-06 — 统一 Job 分配，本地 bub 合条件时优先（修订方案）

**用户决定：** “走统一的 job 分配流程，只是优先分配到本地 bub worker，不合条件再分配到其他
worker”；此前已确认 bub 使用单推理槽。
**状态：** 调度方向已明确；以下实现方案待确认，未修改产品代码、测试或远端 PR。
本条取代变更 008 的独立推理调度入口、禁止回退、不建立 worker 记录和不持久化推理 Job。
更早记录中的“不增加推理 Job”和“通知不可能关联推理 Job”等限制也不再适用。

#### 统一范围与保留的业务区别

- 推理成为真实的 `JobKind.INFERENCE`，进入现有 `jobs` 表。入队、领取、身份校验、结果持久化、
  重试与过期回收均接入现有 Job 流程；不增加第二套推理队列、候选池或轮询调度器。
- 扩展 `WorkerStore.assign_job` 的能力与槽位筛选，在同一个领取入口按 Job 类型分配；
  本地 bub 与外部 worker 使用同一入口。只把另一个 `assign_inference_job` 搬进同一文件，
  或只复用 `jobs` 表、仍由 Pool 选 worker，都不满足本次要求。[J1][J2]
- 编码 Job 保持现有 Flow 与 worker 的绑定、公平分配、租约和停止安全规则。
  推理 Job 可关联 Flow/batch 作为来源，但不创建或改变 `flow_executions`，不签发 GitHub 凭据。
  无 Flow 的通用推理也能入队；允许仅推理 Job 的 `flow_id` 为空，不创建虚假 Flow 或仓库。[J1][J3]
- 普通 worker 保留编码槽与独立推理槽，本地 bub 只有一个推理槽。两个槽由同一持久分配状态管理，
  一个槽的领取/完成/失败不能清空另一个槽；连接在线状态与槽占用分别判断。
  不为本次泛化任意数量资源槽或引入可插拔调度框架。
- 删除 `InferenceRouter`、`InferencePool` 和两层之间的 `WorkerInference` 接口；其必要的失败分类、
  尝试上限和验证迁入现有 Job 路径。保留 Session/Dispatcher 和其他调用仍使用的 `request_reply`。

#### 本地优先及“不合条件”的拟定含义

优先级在领取事务中应用，避免外部 worker 先轮询就抢走应分给本地 bub 的任务。
本地身份及优先级由 controller 授予，网络 worker 不能通过自报字段或同名注册取得该身份。

| 分配时的情况 | 拟定行为 |
| --- | --- |
| bub 配置预检通过、会话就绪、推理槽空闲，支持请求 | 优先分给 bub。 |
| bub 未配置、半配或依赖预检失败 | 记录安全的不可用原因，不把 bub 作为候选，选择其他 worker。 |
| bub 推理槽忙、离线、未就绪、draining、能力不匹配或已被本 Job 排除 | 选择其他合条件 worker，不专门排队等 bub。 |
| 有多个合条件普通 worker | 使用统一分配入口的公平策略；重试保留避让已失败 worker 的规则。 |
| 所有 worker 都不合条件 | Job 保持 QUEUED，在总截止前等待；排队和预检跳过不消耗尝试。 |
| 总截止已到 | 结束本次 Job，不再下发到任何 worker；零次尝试不能通知成“三次失败”。 |

配置预检不付费调用模型，也不能证明凭据、配额和远端服务实时可用。[J4]
“不合条件回退”发生在调度阶段；实际调用后的错误仍按已有分类处理。可恢复失败重新进入同一
分配入口，遵守剩余次数、剩余时间和 worker 排除规则；鉴权、非法输出等永久错误不会因切换
provider 被自动改成可重试。保留 provider 的 `retry_after_seconds` 和既有小退避，不增加熔断器。

#### Job 生命周期、恢复和通知

- 冻结通用请求、逻辑调用键、首次入队时间和总截止；同一正在进行的逻辑调用重复入队，返回同一
  Job，不能重置次数、改写请求或延长预算。排队等待计入总预算；跨重启使用可持久化截止时间。
  编码派生 Job 原有 `source_job_id/source_key` 的语义保持，不拿推理 batch 冒充 source Job。[J3]
- 一个推理 Job 对应原先的一次逻辑调用，最多三次总尝试（含首次）。统一 Job 状态机是唯一重试
  所有者；业务调用方不再嵌套重试。领取后进入授权派发阶段记录本次尝试，发送结果未知也计入，
  UI 不把已授权派发次数描述成已证实完成的模型调用次数。
- 请求和结果携带 Job、分配 token、连接 epoch 与单次请求身份。旧 epoch/token、取消或已终结
  Job 的迟到结果不得应用。失联不证明执行已停；未知结果保留排除规则，可能重复计算/计费。
- 统一结果受理先保存有类型的推理结果，并在 controller 的结果处理阶段完成现有输出和业务验证；
  review 使用现有 `TriageVerdict`，非法业务结果计入本次尝试。未经验证不能把 Job 标成成功。
  不持久化 Python 回调，也不把推理输出伪装成编码结果或建立通用插件式验证器框架。
- review 入队在 batch 当前性检查下幂等完成；轮询只恢复/观察正在进行的 Job。保留
  `review_batches.job_id` 关联修订 Job 的原语义，推理另有显式关联；避免填入推理 Job 后
  `pending_triage/current` 永远看不到批次。业务等待不得阻塞 Session driver 或后续批次入队。[J5]
- **保留既有下一轮行为：** 三次可恢复失败后，本 Job 失败、批次仍为 READY，后续 sweep 可按原规则
  开启新的逻辑调用和新的 Job；这不是重开已失败 Job 或在同一次调用中执行第四次。新一轮身份和
  代次要持久化且并发幂等；不因普通轮询/重启为未完成调用额外生成 Job。三次上限不代表一个批次
  永远最多调用三次。永久错误仍进入 WAITING，新有效反馈仍走原批次规则。[J6]
- 耗尽摘要持久保存并复用现有通知 key/version 更新规则，后续轮次更新同批次通知，不每轮新增
  相同卡片。通知写入失败只补写通知，不能因此生成新推理 Job。已保存答案无 worker 也能恢复。
- 推理超时、失败或取消不能触发编码恢复中“Flow 失败、释放仓库绑定、清空编码槽”的副作用；
  共同结果/恢复入口按 Job 类型执行必要的业务处理，不另设推理 sweeper。[J7]
- 通用 Job 控制消息负责受理、完成和取消；infer-only 指只提供 infer 业务执行能力，不免除控制协议。
  推理取消不能断开还在编码的共享会话，也不能借用绑定 Flow 仓库的 stop 身份。执行槽在确认清理后
  才重新可用；断线/重启隔离旧身份，取消本地进程后 join，再清理临时目录。

#### 附属 worker 和实施顺序

1. **数据与合同：** 更新 `domain/models.py`、`contracts/protocol.py`、结果收据及 `persistence/schema.py`。
   增加推理 Job、可选来源、固定双槽/能力状态、幂等键、截止和失败摘要；添加对既有数据库可重复
   执行的升级，保持已有编码数据和收据可读。通信版本同步升级，拒绝混用不兼容版本。
2. **共同调度与生命周期：** 修改 `persistence/jobs.py`、`workers.py`、`database.py` 的领取/锁序/完成，
   及 `domain/recovery_policy.py`、`controler/workers/callbacks.py`、`workflow/results.py` 的控制入口。
   抽出必要的共用 Job 控制职责，编码专属 preparation、GitHub effects、Flow tools 只在编码路径挂载。
3. **bub 接入：** runtime 托管一对有界进程内 Transport 与 Session/Dispatcher；本地 worker 有真实
   worker 记录和受控身份，通过共同注册与 Job 分配，只挂 infer 业务处理。bub 适配器和 SDK runner
   放在 worker 侧，不调用完整编码 Runtime、不建立 Flow 仓库、也不新增端口或部署单元。
   SDK 仍用短命子进程；凭据留在执行侧配置，不进入 Job、协议、通知或日志。
4. **调用方与记录页：** review 的 prompt/判定逻辑继续由 controller 持有，改为提交/恢复推理 Job。
   执行记录能区分推理 Job 的排队、尝试和结果，继续展示失败通知；审计 latest-job、Flow 阶段、
   编码重试、PR reopen/archive、取消及分页查询，防止推理 Job 被误当成最后一次编码执行。
5. **删除旧路径并验证：** 删除直接 bub 调用、Router/Pool 和失效测试/文档引用；不保留旧独立调度
   作为 fallback。原生 Codex/Amp 的新会话、非 Git 工作区和只生成答案的边界不变。

#### 验收标准（尚未执行）

- 使用真实数据库及双方通信驱动，证明本地 bub、Codex、Amp 都由同一 Job 领取入口下发；
  bub 合条件时优先，半配/忙/离线/能力不符时其他 worker 可领取；bub 永远拿不到编码 Job。
- 并发领取同一 Job 只有一个有效分配；重复扫描/重启不改活动 Job 的预算、次数和冻结请求。
  无 Flow 推理可执行，review 推理可归属其 Flow 详情；不创建或窃取编码仓库绑定。
- 同一 worker 的编码与推理并行，单推理槽不超额；推理超时、取消、重连不干扰编码心跳、停止、
  结果确认或现存 workspace。测试代码结果未知与纯推理结果未知的恢复区别。
- 同一 Job 连续失败最多三次，无第四次；成功、永久失败、预算耗尽和零候选分开验证。
  READY 后下一轮使用新 Job 且幂等；第三次非法业务结果、摘要补写、同 key 通知更新不丢失。
- 保存结果但未保存决定、保存决定但未应用业务效果等崩溃点恢复，不重算已保存且有效的答案，
  不重复 GitHub 效果；旧 token/epoch、取消和旧上下文结果被拒绝。
- 既有数据库升级及编码 assignment/recovery/stop/history/Flow retry 回归；后端测试、Ruff、mypy、
  前端单测/类型/构建和浏览器回归。实际渲染桌面/手机的推理 Job、排队与失败通知，检查截图。

**取舍与咨询复核：** 这是核心 Job 调度改造，范围大于只合并 Router/Pool；不预先承诺总行数下降。
Claude Opus 5.5 的只读计划咨询已完成，初始化和响应模型一致、tools/MCP 为空、退出成功。
未采纳其另设领取/结果通道、所有推理强制无 Flow、耗尽后停放等建议：分别违反统一入口、详情
归属或既有 READY 后继续扫描的要求；也未采用“凭据仅存在子进程”的不准确表述。
本记录经过源码核对，但未执行本修订的产品测试；旧 PR 的通过结果只适用于旧实现。

#### 本次源码依据

- [J1 — 原有 Job 领取与 Flow 绑定](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/persistence/workers.py#L178-L283)
- [J2 — 现有调度与编码下发](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/controler/workers/callbacks.py#L226-L290)
- [J3 — Job 入队与数据结构](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/persistence/jobs.py#L103-L147)、[schema](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/persistence/schema.py#L41-L103)
- [J4 — bub 配置预检](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/inference/bub.py#L28-L80)
- [J5 — Review 当前性与 job_id](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/persistence/reviews.py#L382-L403)
- [J6 — 后续批次扫描](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/controler/runtime.py#L226-L256)；本记录变更 003–004 的 READY 与通知约定
- [J7 — 编码过期回收副作用](https://github.com/b1indsight/agentflow/blob/371ad514c88c73fe0787f483f5fdab3e4c7dc9cb/app/persistence/workers.py#L695-L810)

### 010 — 2026-11-06 — 普通 worker 与 controller 管理的 bub mini worker

**用户澄清：** 手动部署的普通 worker 同时提供 `worker.execute` 和 `worker.infer`，允许不绑定
任何 repo，在执行 infer 时使用空 workspace；bub 是由 controller 管理、只有空 workspace、
只提供 `worker.infer` 的 mini worker。
本条明确变更 009 的角色与 workspace 边界，不另生成一套调度方案；实施仍待确认。

| 角色 | 管理方式 | 业务方法 | 工作目录 |
| --- | --- | --- | --- |
| 普通 worker | 用户手动部署，使用已有 Codex/Amp | `worker.execute`、`worker.infer` | execute 使用编码任务对应仓库；infer 使用该次独立的空 workspace。 |
| bub mini worker | controller 创建、注册、管理和关闭 | 仅 `worker.infer` | 只使用该次独立的空 workspace，不准备编码仓库。 |

- repo 绑定不是普通 worker 上线或接收 infer 的前提。普通 worker 没有任何 Flow/仓库绑定时也能
  接推理 Job；已有编码仓库时，infer 仍不能复用该仓库或编码会话。
- 两种 worker 的每次 infer 都在初始为空、非 Git、互不复用的任务目录中运行。执行期间可写入
  必要的临时输入/输出文件，结束后清理；不 clone/checkout repo，不带入前次任务内容。
  “空 workspace”不表示 OS 沙箱，也不改变既有 trusted-host 安全边界。
- “其他可推理 worker”指上述普通 worker 中当前满足推理准入条件的实例，不是另一批专用部署。
  方法已提供与当前可分配是两个条件；仍需会话就绪、后端可用、能力匹配和推理槽空闲。
- 仍走统一 Job 分配：infer 优先合条件的 bub，不合条件时选普通 worker；execute 只分给普通
  worker。既定双槽、重试、通知和 READY/WAITING 规则不变。
- 验收补充：普通 worker 零 repo 绑定即可执行 infer；其 execute 与 infer 使用不同目录/会话；
  bub 不创建仓库 workspace、不接 execute，且生命周期由 controller 完整回收。

**交付状态：** 本轮仅同步用户定义到本地功能记录及说明图，未修改产品代码或测试、未提交或推送。

### 011 — 2026-11-06 — 统一 Job、双槽与受托管 mini worker 落地

**需求与实际实施：** 按已批准的变更 009–010 重构，取代变更 005 的 Router/Pool 调度。
没有新增独立推理队列、调度循环、部署端口或旧路径 fallback；普通 worker 的编码绑定保持原规则。

- `domain` / `persistence`：新增 `JobKind.INFERENCE`、冻结 `InferenceJob`、可选 Flow 来源、
  `review_batches.inference_job_id` 和 worker 的独立推理槽。只有推理允许无 Flow；普通编码
  入口用 `required_flow_id` 明确检查权限。schema 幂等升级，不改写既有编码收据。
- `WorkerStore.assign_job` 统一领取两个固定槽，复用 `_claim_job` 写入分配身份与尝试计数；
  推理不建立 `flow_executions`。先避让已失败候选，再优先合条件的 bub；未知结果排除原 worker。
  领取事务按 worker ID 锁定状态，确保外部先 poll 也不能抢走应给 bub 的任务。该锁会串行化
  跨 worker 领取，是一致性与吞吐的取舍；不引入调度框架，也不宣称已验证大规模 worker 吞吐。
- `Database.lock_workflow` 在 worker → Flow → job 锁序中核对双槽归属；并发新领取改变 owner 时
  回滚当前 savepoint 后重新发现，避免持有 Flow 锁再补锁新 worker。共同 sweep 区分编码和推理，
  不把推理超时写成 Flow 失败或释放编码绑定。
- `workflow/JobInference` 只负责提交和读取；pending 让出 review 扫描，不阻塞后续批次。
  原 task、总截止、派发次数、结果与通知摘要持久化；三次总尝试包括已授权但结果未知的派发。
  generic 同 request_id 改变输入、验证或来源会拒绝；review 重建 prompt 时仍恢复原活动 Job。
- 共同 `record_assignment_result` / `ResultApplication` 接受推理结果，先保存，再验证身份、
  字节限制、JSON 与 `TriageVerdict`，最后提交 DONE 或失败/重试。业务验证不放在成功之后。
  通知和终结共用事务，补写失败保留 inbox；已保存答案不依赖 worker 在线。
- `app.inference.local` 管理仅 infer 的 mini endpoint，`app.inference.transport` 在两类 worker
  之间共享后台推理、心跳和取消。mini 通过有界进程内 Transport 和实际 Session/Dispatcher
  注册，host 使用同一 ServerCallbacks；不实例化编码 WorkerRuntime。controller 不导入 worker
  包，两个旧推理模块已移除，无 shim。bub 凭据仅在本地适配器配置和短命 SDK 子进程间传递。
- wire19 注册增加 execute 能力，infer 请求/回复携带 job/token；取消按相同身份确认，cleanup
  完成后才能复用槽位。推理-only 心跳维持连接，不延长任务总预算；编码同时运行时不能代替编码
  心跳。纯推理连接无响应会回收未知尝试，不能长期占槽；关闭与重复取消仍等待 provider 回收。
- 控制台显示推理 Job 和派发次数，通知明确次数包含结果未知的派发。Flow 阶段、最新编码诊断、
  archive/reopen/stop 查询排除推理任务，NULL Flow 的通用任务不会触发无效 console 投影。
  README、组件职责、部署和 wire 说明同步更新。

**联调中修正的边界：** malformed 回复从暂态 RPC 改为 Job 后不能只拒绝已不存在的 Future；
现在通过已授权的 request_id 保存 `invalid_response`，不会等待截止再错误地重试。
超大 `Retry-After` 先和剩余预算比较，避免 datetime 溢出卡住 inbox。
上述问题均使用失败用例复现后修复；没有放宽包边界测试或类型检查。

**当前交付：** 修改仅在本功能独立 jj workspace，本轮未提交、推送、更新远端 PR、合并或部署。
原生工具权限仍依赖部署隔离；空 workspace 不构成沙箱，断线重算可能重复计算和计费。

### 012 — 2026-11-06 — 统一调度的集成回归与消融

**实际验证：** 在独立工作区使用专用 PostgreSQL，完成以下检查；不沿用旧发布版本的结论。

- `FLOWTOOL_TEST_DATABASE_URL=… uv run pytest -q` 全量通过、无跳过，包括实际双端 Session 与
  PostgreSQL 的 mini 优先/忙时转派、无 repo 推理、编码并发和取消、心跳/静默断线、持久结果恢复、
  三次上限、READY 新一轮、永久 WAIT、通知补写、schema 重复升级及编码 stop/history 回归。
- `uv run ruff check app tests`、`uv run mypy app tests` 通过。
- Web 目录 `npm test`、`npm run typecheck`、`npm run build`、`npm run format:check`、
  `npm run test:e2e -- --workers=1` 全部通过。沿用单 worker 避免变更 007 已确认的既有并发偶发问题。
- `uv run python scripts/build_console.py` 后执行 `uv build --no-sources` 成功；核对 wheel 包含新
  mini/shared callback 模块、没有已删除模块，console_static 与本轮前端 dist 逐文件一致。
- 桌面和手机的推理四状态、派发次数和失败通知截图均已查看，无遮挡或横向溢出；协议文档卡片
  的 DOM 和截图确认 wire19。浏览器用 API 响应夹具；数据库、通知 API 和分配集成由 pytest 验证。

**验证期间的失败及处理：** 首轮架构检查暴露 controller 导入 worker，已将共享推理与 mini
生命周期移到 `app.inference`，边界测试未放宽。最初同时运行 pytest 和 Playwright 导致 CLI 安装
测试拷贝源码时碰到 test-results 删除；之后顺序运行完整两套测试，通过。静态包核对最初使用了
错误的归档路径，进一步发现缺少正式 console 拷贝步骤；执行仓库原有构建脚本后重新核对通过。
保留既有 Vite 大 chunk 提示及 TestClient 弃用提示，未为消除提示更换依赖。

**消融方式：** `.amp/in/ablate_unified_inference.py` 通过 `inspect.getsource` 在独立 Python
进程中替换单个方法的一项机制，再运行同一个行为用例；产品文件不修改，不留测试开关。
基线通过，以下变体都使对应断言失败：

| 移除或简化 | 观察结果与决定 |
| --- | --- |
| 删除 bub 优先排序，只按公平等待顺序 | 已就绪空闲的 bub 被刚上线的普通 worker 抢先；保留事务内本地优先。 |
| 重启恢复时重置尝试数 | 第二次授权又被记为首次，无法遵守同 Job 的三次总上限；保留持久预算。 |
| 跳过结果应用中的 review 校验 | 空答案等非法业务结果被标成 DONE；保留成功前校验。 |
| 取消不核对 job/token | 外来取消中断当前 provider；保留精确身份校验与 cleanup 占槽。 |
| 推理沿用编码槽的空闲要求 | 正在编码的普通 worker 不能接推理；保留固定双槽。 |

复现：恢复[验证附件](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)
中的脚本源码到上述路径，设置测试数据库后运行 `uv run python .amp/in/ablate_unified_inference.py baseline`；
分别以 `priority`、`budget`、`review`、`cancel`、`slots` 替换 baseline。基线退出 0，变体退出 1。
执行后删除临时脚本，仅在附件保留源码和日志。带锁 owner 重发现由受控并发 PostgreSQL 用例验证；
尚未测量大规模 worker 吞吐，不以此证明锁成本为零。

**限制与交付：** 没有使用真实付费模型、GitHub 写入或生产部署做 smoke；bub SDK 的假端点
集成和原生进程清理通过自动化。新架构需 serve/worker 一同升级 wire19。本轮未提交或推送，
旧 PR 的代码与 CI 不代表本轮重构已发布；新审查门禁与 PR 更新留待发布授权。

### 013 — 2026-11-08 — PR 上下文失效时解除旧推理结果关联

**触发与范围：** 用户针对发布审查第 1 项确认 head/base 比较含义后要求修复。
原实现清空 review context/decision，却保留已完成的 inference_job_id；下一轮会复用旧答案并
被 ReviewJudge 标上新上下文 digest。本次仅修复该结果复用问题，不修改另外两项心跳竞态和
编码准备阻塞推理的问题，也不改变 head/base 任一变化即失效的既有判定。

**实现与取舍：** `ReviewStore.invalidate_context` 在原有受保护的 UPDATE 中一并清空推理
关联，保留匹配旧 snapshot 和 `NOT answer_dispatched` 条件；旧请求不能清除较新的上下文，
已授权但结果未知的 GitHub 回复仍须先对账。`JobStore.enqueue_inference` 复用已有 review Job
前核对其冻结的 context digest，拒绝将持久化的旧关联复用到新 snapshot，改为创建新 Job。
原 Job 保留为历史；相同 snapshot 的结果恢复、活动预算和失败重试规则不变。正在运行的旧
推理仍由已有取消流程回收，清理完成前保留 worker 槽，不增加新的状态、协议或调度器。

**验证：** 先用真实 PostgreSQL、真实 coordinator/ReviewJudge/JobInference/结果应用与受控
GitHub 资源复现，再实施修复。head/base 分别变化，answer/revise 两条效果路径均在修复前
错误地复用旧结果；修复后等待新 Job 完成，才回复新答案或创建包含新指令的修订任务。
上下文未变、重新构造调用方时仍直接恢复保存答案；持久层另验证旧关联与新 context 共存时
不会误用已完成结果。相关 pytest、完整 PostgreSQL pytest、全量 Ruff 和 mypy 均通过。
本轮不涉及 UI、构建配置或依赖，未重跑前端浏览器和 wheel 构建；无真实 GitHub/provider 写入。

**边界与消融：** 临时测试确认旧 snapshot 的迟到失效请求和未知回复保护仍保留当前关联，
运行中旧推理仍占槽直到 cleanup。仅在独立 Python 进程中替换方法：去掉清空关联，失效后
仍残留旧绑定；去掉恢复时的 digest 检查，会再次返回另一 context 的 DONE 结果。对照均失败，
完整实现均通过；不修改产品文件或保留测试开关。临时检查源码与日志保存在
[验证附件](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)，可执行临时测试已清理。

**交付：** 本修复保留在独立 jj 工作副本，尚未推送至 PR；发布前仍需运行审查门禁。
先前发布提交的 CI 已通过，不代替本修复的发布验证。未合并或部署。

### 014 — 2026-11-08 — 接收端忽略已确认结束任务的旧心跳续租

**触发与范围：** 用户针对第 2 项要求先实现接收端处理：确认旧心跳属于已结束的任务后，
直接忽略任务续租。本次只修改 `ServerCallbacks` 的控制校验与续租入口，不调整发送端、wire19、
任务预算或调度器，不拆分连接心跳协议，也不处理第 3 项编码准备阻塞推理的问题。

**实现与取舍：** 在有效 connection epoch 内，读取现有 Job 持久记录，确认状态为 DONE/FAILED、
结果已应用、worker 与 assignment token 相符；推理任务还核对持久化 assignment epoch。
匹配后不进入续租，不刷新共享接收期限，避免旧工作替当前工作保持存活；Gateway 仍逐项
校验并分派 payload，合法的新推理结果照常应用，非法 payload 仍走既有拒收路径。
未知任务、错误身份、未应用结果和 QUEUED/RUNNING 状态不享受此例外。
不增加历史身份缓存或持久表；使用现有终态记录确认身份，每条心跳增加一次按主键读取。
发送端仍可能排队发送旧心跳，本次通过接收端容忍已确认终态解决误断连接。

**验证：** 先在真实 PostgreSQL、实际 Session/Gateway/Dispatcher 中复现：完成或失败的推理后，
旧心跳在没有编码任务、编码等待 Started、编码运行时都会误断连接；随包的新结果被阻断。
修复后，空心跳及携带新推理结果的旧心跳均不续租、不影响当前编码任务；错误 token/epoch
继续拒绝，非法推理输出仍失败且不中断连接。相关回归、完整 PostgreSQL pytest、全量 Ruff
和 mypy 均通过，仅保留既有 TestClient 弃用提示。没有 UI、依赖或构建配置修改，未另跑浏览器
和 wheel 构建；没有真实 provider/GitHub 写入或部署 smoke。

**边界与消融：** 临时检查覆盖编码/推理的成功和失败终态、重复旧心跳、未完成或未应用结果、
未知任务、worker/token/连接 epoch/推理分配 epoch 错误。旧心跳既不调用任务续租，也不推进
接收截止时间。独立 Python 进程只替换方法：删除接收期限保护会让旧心跳延长共享期限；删除
token 核对会放行错误身份，对照均按预期失败。产品文件没有消融开关；临时测试已删除，
源码文本、失败复现、消融及验证日志保留在[验证附件](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)。

**交付：** 本次与变更 013 的修复均保留在原独立 jj 工作副本，未推送、未更新 PR、未运行新的
发布审查门禁，未合并或部署。外层 checkout 的其他改动未混入。

### 015 — 2026-11-08 — 编码准备接入现有 Session.watch

**触发与范围：** 用户针对第 3 项了解现有处理后要求接入。原 `poll` 等待编码准备结束才派发
推理、接收业务事件；慢 GitHub 读取或 capability 签发会阻塞共享连接。本次复用既有 watch/Ready，
不增加调度器、队列、wire 变体或持久状态；工具和结果应用等其他串行回调不在异步化范围内。

**实现与取舍：** `ServerCallbacks` 领取编码 Job 后观察 `_prepare_assignment`，保存唯一准备身份，
立即继续推理派发和消息处理。准备期间仍由 controller 续租，不要求 worker 对未下发任务发心跳。
完成后 `_on_prepared` 在原 driver 串行处理 QUEUED/DONE/FAILED 或构造下发，保留发送前的
`authorize_dispatch` 校验；仅实际下发后设置 current/starting。源协程异常沿用 Session/Gateway
失败路径，不另做 Exception-as-data。停止时 unwatch 并等待准备回收，以原 token/epoch 尝试重排
未下发任务，随后停止此前已下发的执行；Flow 已取消时持久层拒绝重排，不复活被取消的任务。

**清理边界：** 新交叉回归发现，原 unwatch 从观察表移除任务后，close 看不到仍在执行的取消
清理；等待 unwatch 的调用方被取消时还会二次中断清理。本次由 Session 保留正在清理的观察，
将其计入原容量并在 close 时一并等待；用 shield 防止取消等待者中断已启动的清理。完成通知仍
立即失效，不重复取消正在清理的观察。该修正属于既有通信生命周期，不加入业务专用关闭逻辑。

**验证：** 修复前新增的慢准备推理回归及 Codex/Amp 历史读取回归均失败；接入后准备保持挂起时，
推理能派发、续租、完成入库，编码准备租约继续更新，历史 API 返回成功；释放后编码只执行一次。
原有准备重试、停止、发送前身份失效、通信与连接替换回归通过。临时真实 PostgreSQL/Session
检查覆盖 EOF、连接替换、停止、停止与替换交叉、driver 取消；新连接等待旧准备完成清理，旧连接
不下发已撤销的后继任务。准备期提前报 Started 或结果仍拒绝。完整 PostgreSQL 后端测试、全量
Ruff/mypy 和 diff 空白检查通过；保留既有 TestClient 弃用提示。可执行临时检查已删除，仅保留
源码文本和日志。没有 UI 或构建变更，未另跑浏览器和 wheel 构建；未调用真实 provider/GitHub 写入。

**消融：** 独立进程替换一个方法后运行相同输入，不修改产品文件：恢复串行等待会阻塞推理；
删除发送前授权会下发失效任务；删除停止时 unwatch 不会及时回收准备；关闭不等待已撤销观察
会提前返回；删除 shield 会中断取消清理。完整基线通过，各简化变体的相应断言均失败，保留以上
机制。复现脚本、临时生命周期检查及日志存于[验证附件](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)。

**交付：** 与变更 013–014 一起保留在原独立 jj 工作副本；未提交、推送、更新 PR、运行新发布
审查、合并或部署。前两项修复保留，外层 checkout 未混入。本次不涉及 UI、依赖或构建配置。

### 016 — 2026-11-08 — 清理未使用的推理对象与过时说明

**范围与实现：** 用户要求精简 PR 后推送。删除未被读取的 `ServerRuntime.inference`，保留每个
review batch 实际使用的 `JobInference`；删除没有独立消费者的 `InferenceCallFailed` 包装和异常中
未被读取的 worker 身份，直接用 `InferenceError` 返回已持久化失败。失败消费顺序、错误码和
backend 归因不变，通知继续从持久化 summary 读取 worker 身份。同步删除测试中对应的无用
Mock 字段，纠正“每 worker 单槽”、旧 routing 与通知独立于 Job 的过时说明。

**保留与取舍：** 变更 013–015 的身份核对、异步准备及清理所有权均保留。持久化失败列表及
wire/通知错误码不在本轮删除：已有 PR 版本的部署和数据使用情况未确认，删掉严格模型字段
或取值需要单独明确迁移范围。保留 history 使用的有界 request/reply helper；内联只会搬动
发送锁、EOF 和回复竞态处理，并增加测试辅助代码，不减少实际生命周期复杂度。初稿与历史
时间线按记录规则保留。本轮未增加兼容层、模型字段、重试机制或新依赖。

**验证与交付：** 完整 PostgreSQL pytest（无跳过）、全量 Ruff/mypy、diff 检查通过。
临时调用检查确认永久/可恢复失败保留原错误码和 backend，消费一次且不重新派发；未新增永久
测试或保留临时脚本。仅后端与说明变化，未重跑前端页面、浏览器或 wheel 构建；无真实 provider
调用和部署 smoke。与前三项修复一起在同一 bookmark 发布，审查仍使用用户要求的 1200 秒上限；
只有门禁通过才推送它返回的精确提交，实际发布和 CI 状态见 PR 与验证附件。未合并或部署。
