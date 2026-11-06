# Worker 推理任务下发 — Feature record

**Stage:** Implemented — 已实现并完成本地验证，最终验证见变更 007；待 PR 审查与 CI
**PR:** 同名分支的 PR；发布链接与审查结果记录于[交付记录](https://ampcode.com/artifacts/33bac7f9-fd27-4934-9ad6-2e5dabc2b422)
**Bookmark:** `feat/worker-inference-dispatch`（计划和实现沿用同一个 PR）

**当前阅读入口：** [变更 001](#001--2026-11-06--通用推理合同与-bub-缺配置时自动下发)
定义通用合同与自动选路；[变更 002](#002--2026-11-06--复用-codexamp-独立会话与本地答案工作区)
确定原生后端、独立会话及答案生成边界；
[变更 003](#003--2026-11-06--可恢复失败允许同次调用直接重试) 取代此前延后重试的限制；
[变更 004](#004--2026-11-06--执行记录中的推理失败通知) 增加重试失败后的详情通知。
初稿和先前变更保留为历史；冲突处以较新的变更为准。
当前要求：controller 未配置 bub 时，自动选择可推理 worker，复用其 Codex/Amp；
每项请求在不关联 GitHub 的本地工作区启动新会话，只返回答案；可恢复失败允许有界直接重试。
3 次尝试均失败时，在详情的执行记录中显示持久通知。实现及验证记录见变更 005–007。

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
