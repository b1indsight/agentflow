# Flow 会话查询接口 — Feature record

**Stage:** Planning，待用户批准后实施
**PR:** [#58](https://github.com/b1indsight/agentflow/pull/58)

**当前方案：** 以时间线第 1 条修订为准：control 实时代理 worker 查询 Codex 历史，
不新增会话队列、采集上传或持久化。以下初始方案保留为评审历史，已被该修订替代。

## 1. Initial plan

### Summary and motivation

为后续 WebUI 提供按 flow 获取 CodeAgent 会话内容的 control HTTP 接口。目前
`app/control/api.py` 只有 webhook、worker 连接及运维端点；数据库保存 session ID
和结果摘要，而 `app/worker/codex_driver.py` 执行结束后仅从 JSONL 解析 session ID，
未将会话传回 control。单独增加数据库查询不能恢复完整对话。

### Goals, scope, and success criteria

- 查询一个 flow 的所有 attempt、session generation 和执行轮次，返回按持久化顺序排列的
  输入、agent 回复和可展示的工具调用/结果；保留来源和轮次关联。
- 提供稳定的 JSON 契约、游标分页及新增内容轮询；worker 离线、flow 归档后仍可查询已保存内容。
- 新执行从采集开始记录；旧 flow 无记录时明确标记 unavailable，不把缺失内容描述为完整空会话。
- 本次不实现 WebUI、SSE、会话写入接口、GitHub 讨论聚合或旧 worker session 文件回填。
- 仅记录 agent 对外输出事件，不读取内部隐藏推理或将 worker 环境、认证配置作为会话内容暴露。

### Proposed behavior

`GET /api/flows/{flow_id}/conversation?after=0&limit=100`

- 使用独立的 `FLOWTOOL_CONTROL_API_TOKEN` Bearer 认证；未配置时端点禁用并返回 503，
  错误或缺失凭据返回 401。该 token 只属于 control，不复用 worker/GitHub 凭据。
- 返回 `flow_id`、`items`、`next_cursor`、`has_more` 和各执行轮次的记录状态。
  `items` 包含稳定 `id`、`attempt_id`、`job_id`、`invocation_id`、可空 `session_id`、
  `session_generation`、时间、事件种类和结构化展示内容；种类覆盖输入、回复、工具调用、
  工具结果及执行错误，不向 UI 暴露任意原始协议对象。
- `after` 为该 flow 的非负事件游标，`limit` 为 1–500；不存在的 flow 返回 404，
  参数非法返回 422。已存在但无历史记录返回 200、空 items 和 unavailable 状态。
- 游标依据单 flow 的持久化序号递增，不能依据时间戳或未提交的全局序列推断完整性。
  `next_cursor` 为最后一条返回记录的 ID，空页保持输入游标；`has_more` 由多取一条判断。
- 记录状态区分 collecting、complete、partial、unavailable；complete 只表示本轮可展示
  事件已传输完毕。截断、超限、异常中断和未知格式需显式标记，不能静默显示为完整记录。

### Technical design and affected modules

1. `app/contracts/` 定义有界会话事件、批次及确认消息。每次实际 agent 调用有独立
   invocation ID；session resume 拒绝后的 fallback 是另一调用，避免混淆两次输入。
2. `app/worker/codex_driver.py` 和 executor 从实际提交的 prompt 与公开 JSONL 事件采集；
   增量消费 stdout，并继续消费 stderr，保持现有结果解析、超时、取消和 session fallback 行为。
   使用允许列表规范化事件；限制事件及批次字节大小，过大内容提供截断标志。
3. worker 先把规范化事件写入本地持久 outbox，再通过现有 WebSocket 分批发送。
   control 提交后确认；仅确认后删除本地批次。断线按 invocation/sequence 重传，
   不将完整历史塞进 terminal result，也不依赖清理后不复存在的 workspace。
   会话传输与 heartbeat/取消共用连接但不阻塞其处理；存储失败必须可观测且记录不完整状态。
4. `app/control/gateway.py` 验证 worker、assignment token、connection epoch 与 job 归属，
   flow/attempt 由已验证 assignment 推导。会话确认独立于业务执行成功；补传只接受可验证的
   已有 assignment 身份，不能让过期连接写入新执行。终态确认及归档清理不能提前丢弃未确认 outbox。
5. `app/persistence/` 新增独立会话 store 和 additive schema。以 invocation/sequence 唯一约束
   实现幂等，相同标识但不同内容拒绝。锁定 flow 的计数行分配连续序号并在同一事务写入，
   使已返回游标不会跨过稍后才提交的记录；建立 flow/序号索引，查询不执行全表加载。
6. `app/control/api.py`、runtime 和 config 注入只读查询入口，使用 Pydantic 响应模型。
   数据库读取使用仓库现有异步边界，避免阻塞事件循环。更新部署配置及接口说明。

### Alternatives and rationale

- 仅返回现有最终摘要：改动最小，但无法满足多轮会话和工具记录展示。
- 请求时代理 worker 读取 session 文件：依赖 worker 在线和本地文件格式，归档后内容消失，
  不适合作为 WebUI 的稳定查询来源。
- 选择 control 持久化：增加传输和存储成本，但查询不依赖 worker，复用现有连接与恢复身份。
  本地 outbox 和确认机制只用于会话批次，不引入通用消息系统。

### Costs, risks, and mitigations

- 会话可能很大：逐条及逐批限额、数据库分页、截断标记；历史保留/压缩策略后置，记录磁盘增长风险。
- 内容可能含源码或敏感输出：Bearer 认证默认关闭；发送前过滤已知执行凭据，
  不保存原始 assignment secret 或环境变量。不能承诺自动识别 agent 文本中的所有敏感信息。
- 协议变更需要协调部署 control/worker，不能假定旧 worker 自动支持采集；协议兼容处理及升级顺序
  在实施时依据现有版本检查明确，旧数据保持 unavailable。
- 使用公开输出只能提供运行期间捕获的记录，不能承诺还原部署前或未采集的历史。
- 新增表采用 additive 迁移；回退应用不删除已记录数据。认证 token 不嵌入未来前端静态资源。

### Implementation order

1. 定义响应示例和事件规范，补充接口、分页和数据归属失败测试。
2. 实现会话 schema/store 和 control 查询接口。
3. 测试先行实现 worker 采集、outbox、协议确认与恢复；覆盖 resume/fallback 和取消。
4. 补充跨 worker/control 的集成测试及部署/API 文档，执行消融和代码审查。

### Validation and ablation strategy

- 验证认证、404、422、空历史、跨 flow 隔离、跨 attempt/session 顺序、分页追加及归档后查询。
- 验证真实 PostgreSQL 并发提交不会丢记录或越过游标；重复批次幂等，冲突及伪造归属拒绝。
- 使用固定公开事件样本验证输入、回复、工具输出、未知格式、超限和凭据过滤；进程测试覆盖
  大量 stderr、超时、取消及 resume fallback，不依赖实时模型调用。
- 集成验证断线重传、control/worker 重启、终态确认与未确认批次交错，确保现有执行恢复不回归。
- 实施后、代码审查前分别移除幂等约束、归属校验和按 flow 的提交序号保护，验证对应测试失败；
  比较仅依赖最终结果携带记录的简化方案在取消和断线样本下的缺失情况。
- 执行 Ruff、Mypy、相关测试、PostgreSQL 全量回归及 `uv build --no-sources`。

### Open questions and deferred work

- 默认“会话”指 CodeAgent 的输入、回复和工具记录；若需要 GitHub Issue/PR 讨论聚合，
  在批准前调整本方案范围。
- 具体公开事件字段、既有协议版本升级方式和 outbox 存储布局在实施中由现有契约与固定样本确定。
- 实时推送、多用户权限、历史 session 导入、保留策略和 UI 展示后续独立设计。

## 2. PR change timeline

### 1. 2026-09-30 — 改为 worker 历史实时查询

**来源与状态：** 用户明确要求“暂时不搞队列和持久化，前端发出请求，由 control 转换为
对对应 worker 的查询请求，worker 查询 codex 历史，然后返回结果”。该架构约束已确定；
下述接口细节待评审，实施尚未开始。

**替代范围：** 本条替代初始方案中的主动事件采集、outbox、批次确认与重传、control 会话
表与序号分配、离线可读承诺，以及相关测试/消融。不新增会话数据持久化，继续读取已有的
flow、attempt、session ID 和 worker 归属元数据。Codex 自身已有历史是唯一会话内容来源。

**交互：** 前端 HTTP GET → control 查 flow 归属与已知 session → 现有 WebSocket 发只读
查询 → 对应 worker 读取 Codex 历史并规范化 → WebSocket 返回 → control 返回 HTTP JSON。

- 保留 `GET /api/flows/{flow_id}/conversation`，使用 `cursor`（可选、不透明）和
  `limit`（默认 100，范围 1–500），不再使用数据库事件序号 `after`。
- control 使用已有 `get_flow_execution` 和 `list_attempts` 定位 worker 与已知 session；
  客户端不能指定 worker、任意 session 或文件路径。已释放的绑定仍可用于定位原 worker，
  但不代表历史一定还在。
- 响应包含 `flow_id`、`items`、`sessions`、`next_cursor`、`has_more`；items 提供 session、
  attempt、种类、内容和可用的时间信息，稳定条目 ID 由 session 和源记录位置确定。
  sessions 逐个标记 available、not_found 或 unsupported；不再承诺采集完整性状态。
- 按 attempt 顺序读取并去重已知 session，同一 session 内保持原始记录顺序。游标绑定 flow、
  session 身份及源读取位置，校验输入且禁止作为任意路径使用。读完现有记录仍返回可续读游标，
  供前端轮询；session 被替换或源文件失效时返回 `409 cursor_invalid`，由前端重新加载。
- `has_more=false` 仅表示已读完当前可见历史，不代表 agent 执行结束。运行中的历史只读取
  已完成记录，文件末尾未写完的一条留给后续请求；无法支持的格式明确报告。

**内部请求响应：** 增加只读 `conversation_query` / `conversation_result` 消息，以
`request_id` 关联。control 维护有上限、仅内存中的等待请求映射，限定超时；这是请求关联，
不引入任务队列、持久化、自动重试或离线补传。响应必须匹配原 worker、connection epoch
和请求 ID，重连后旧响应不能满足新请求；HTTP 取消、超时、断线均清理等待状态。

worker 忙碌与空闲时都需处理查询，不能并发调用同一 WebSocket 的 recv，也不能让历史读取
阻塞 heartbeat、任务结果或 stop 消息。通过现有消息分发路径处理响应，worker 将有界文件
读取放到事件循环外；限制请求并发、扫描量、条目大小及响应字节数，超大条目显式标记截断。

**Codex 历史适配：** worker 使用与执行进程一致的 Codex 历史位置，只查询 control 指定且
经过格式校验的 session，校验源元数据的 session 身份；路径解析须限制在历史根目录内。
实施前核验当前 Codex 支持的只读历史接口或本地记录格式，再固定适配和脱敏测试样本，
不假定存在未经验证的 CLI history 子命令。返回用户消息、agent 回复与工具记录的允许列表，
不返回原始环境/凭据、隐藏推理或无关 session 数据。

**错误与限制：**

- 保留独立 control Bearer token：缺失/错误认证 401，未配置服务 token 时 503。
- flow 不存在 404；非法参数 422；worker 离线/断线 503（`worker_unavailable`）；
  worker 查询超时 504；无法读取或解析的上游故障 502，不能伪装成空历史。
- flow 尚无绑定或已知 session：200 空记录并说明 unavailable 原因。
  部分 session 文件不存在：200，逐 session 标记 not_found，其他已知 session 仍返回。
- 仅查询已有元数据可定位的历史。当前 attempt 的 session ID 在结果写回前可能尚不可用，
  session fallback 已覆盖的旧 ID 也不能保证恢复；不为解决这些问题新增历史索引或事件采集。
- worker 离线或本地历史被清理时无法展示；不增加服务端缓存和归档副本。前端可主动重试。

**实施与验证（替代原顺序）：** 先固定 API/协议及失败行为测试，再实现 worker 历史读取
适配、WebSocket 请求关联、control 查询端点与部署文档。测试覆盖跨 flow/session 隔离、分页、
活跃文件尾行、缺失/损坏历史、超时/取消清理、断线/旧 epoch 响应、忙碌 worker 及限额。
消融移除响应来源校验和路径边界校验，确认相应测试捕获错误；不再做持久化相关消融。
执行 Ruff、Mypy、相关测试、现有 PostgreSQL 回归及构建，保持同一 bookmark 和 PR。
