# Worker Amp CLI 适配 — Feature record

**Stage:** Implementation — Amp v16 已重放到收窄后的取消回收基线，本地组合验证 938 项通过；依赖仍有已披露 high，目前不建议合并，Docker/Linux 与线上 GitHub 闭环未验收。
**PR:** [Draft PR #107](https://github.com/b1indsight/agentflow/pull/107)，依赖 [PR #109](https://github.com/b1indsight/agentflow/pull/109) 的取消回收合同；原 bootstrap [PR #103](https://github.com/b1indsight/agentflow/pull/103) 已合并，Amp 未合并或部署。
**Bookmark:** `feat/worker-amp-cli`

## 1. Initial plan

### 背景与目标

worker 已通过 [`CodingAgent.start/resume`](../../app/domain/ports.py) 隔离编码后端，
但 [`WorkerRuntime`](../../app/worker/runtime.py) 固定装配 Codex。新增 Amp CLI adapter，
让运维按 worker 选择 Codex 或 Amp，沿用现有 INITIAL → REVISION → ARCHIVE 闭环。

当前还有三处相关约束：

- [`executor`](../../app/worker/execution/executor.py) 依赖严格 JSON 结果、独立 VCS 核验，
  INITIAL 还必须完成 worker MCP 的 `flow.open_pr`，不能只替换可执行文件名。
- [`会话契约`](../../app/contracts/conversation.py) 和
  [`ConversationQuery`](../../app/contracts/protocol.py) 只接受 UUID；
  [`WorkerSession`](../../app/worker/state/session.py) 固定读取 Codex rollout。
- [`worker 镜像`](../../Dockerfile) 仅安装 Codex、Git/jj 等现有执行工具。

Amp 的公开接口支持本地 execute、stream JSON 和按 thread ID 继续；输出含 `T-UUID`
形式的 `session_id`，最终 `result` 事件提供文本与成功/失败信息。
依据：[execute mode](https://ampcode.com/docs/cli/execute-mode)、
[stream JSON schema](https://ampcode.com/docs/cli/streaming-json)。
本机只读检查了 `amp --version`、`amp --help`、`amp threads continue --help` 和
`amp threads export --help`；版本为 `0.0.1792425645-g3c5d47`。
未运行 Amp agent，也未验证真实恢复、模型输出或 MCP 调用。

### 范围与验收标准

1. 默认仍为 Codex；设置 `FLOWTOOL_CODING_AGENT=amp` 后装配 Amp。
2. Amp 可执行 INITIAL，返回真实 thread ID，调用当前 `flow.open_pr`，结果通过既有核验。
3. REVISION 继续指定的 Amp thread，注入本轮 push capability，不能复用过期的 INITIAL MCP。
4. 取消和超时回收该次 CLI 及其子进程；未知执行不得自动重跑。
5. 控制台现有会话 API 能读取 Amp 的公开消息与工具记录，不泄漏凭据或思考内容。
6. Codex 默认行为、任务调度、control 的 GitHub 操作权威和归档规则不变。

本次不增加按任务动态选 agent、跨后端会话迁移、模型路由、Amp orb/runner 执行，
不重命名数据库里的 `codex_session_id` 历史字段，不改 UI 布局或 Judge。
一个 worker 进程固定一个后端；存量 Flow 必须先收尾，再更换该 worker 的后端。

### 拟议行为

配置入口新增 `coding_agent: Literal["codex", "amp"] = "codex"` 与
`amp_run_timeout_seconds = 1200`；现有 Codex 超时配置保持不变。
Amp 认证使用专用 worker 的 `AMP_API_KEY`，不复制个人 HOME、现有登录会话或 control 凭据。
保留 `trusted_host_execution` 与外部 account/container/VM 隔离要求。

调用示意如下，具体选项顺序按固定 CLI 版本校准：

```text
INITIAL:  amp --executor local --execute --stream-json
REVISION: amp threads continue T-<uuid> --execute --stream-json
```

prompt 通过 stdin 写入后立即关闭，子进程 cwd 显式设为 Flow workspace；
不使用最近会话选择器、远程 executor 或执行后主动归档。
start 使用 `--no-archive-after-execute` 保留后续修订所需 thread。
使用 worker 专用 settings，不修改个人、仓库或服务端 Amp 设置。

当前 CLI 帮助未提供 Codex 的 `--output-schema` 选项。Amp adapter 将调用方给定的
JSON Schema 加入本轮提示，要求最终只输出 JSON；从成功的最终 `result.result` 原样返回。
业务字段仍由 executor 的 Pydantic 模型校验，不从任意 assistant 文本提取 JSON，
不自动剥除 Markdown、不生成成功结果，也不为格式错误重跑 agent。
这保留严格接收规则，但不承诺模型侧的 schema 约束生成。

### 技术设计与受影响模块

#### 配置与执行适配

- `app/worker/config.py`、`runtime.py`：按配置选择 adapter，并把所选后端的超时用于
  CLI 与整轮 revision deadline；不引入插件注册器或新的 agent 管理层。
- 新增 `app/worker/execution/amp.py`，实现现有 `CodingAgent` 接口，无新增 Python SDK 依赖。
  定义最小 stream 事件类型，校验 init/result 的 session 一致性；resume 必须返回原 ID。
  不完整流、错误 result、无最终 result、非零退出均失败，不能把进程退出 0 单独视为成功。
- `ports.py`、相关模型及 executor 注释去除仅适用于 Codex 的描述；共用业务 prompt、
  capability 验证、VCS 核验和交付回执，不复制 INITIAL/REVISION 编排。
  Amp 的工具名映射与输出 schema 提示由 adapter 负责。
- stdout 按行消费、stderr 并行排空并有界保留，避免长工具输出占满内存或堵塞管道。
  不请求 thinking 流。超时/取消处理进程组并等待退出，临时配置在所有退出路径清理。

#### MCP 与凭据

- 复用 `git_credential_environment()`，只传本轮 push token；Amp 环境使用独立 allowlist，
  包含必要系统、代理/证书变量及 Amp 认证，不继承 worker token、数据库、GitHub App secret。
- INITIAL 的 `flow` MCP 使用本轮 URL，Authorization 通过
  `${FLOWTOOL_MCP_TOKEN}` 环境插值；不在 argv、prompt 或持久设置中写入原始 token。
  配置形式以 [Amp MCP 文档](https://ampcode.com/docs/customize/mcp) 为准。
- REVISION 的 resume/fallback 都显式禁用 `flow`，覆盖保存或仓库提供的同名 broker。
  专用 settings 限制工具集，禁止意外接入个人/服务端 MCP、远程协作或管理工具。
  CLI 配置合并和有效工具列表须实际校准；不能仅根据空 MCP 字典断言已经隔离。
- 最终结果、错误详情与历史写入前统一脱敏，包括本轮 push/MCP token、Amp API key；
  不记录完整命令环境、原始 MCP 配置或原始流。

#### 修订恢复

保留 executor 的单次 fallback journal。Amp resume 先校验 `T-UUID`，
拒绝 Codex UUID 和任意路径，不支持自动跨后端重建。
只有固定版本的明确、执行前 thread 不存在拒绝，且没有模型/工具执行证据，
才允许抛出 `SessionUnavailable`。网络错误、超时、鉴权失败和通用错误均不得触发 fallback。
如果公开错误格式无法证明未执行，则保守失败，保留诊断，不猜测 stderr 关键词。
缺 session 时现有一次性 start fallback 行为不变。

#### 会话读取

- 在 conversation/protocol 中复用同一个受约束 session ID 类型，接受原 UUID 和 `T-UUID`，
  继续拒绝路径、URL 及任意字符串；字段名称、分页与错误语义保持不变。
  按现有严格协议版本机制处理 control/worker 的同步升级，不做混合版本适配。
- 不在每次会话读取时调用 `amp threads export`：该命令返回完整 thread，CLI 帮助没有
  有界分页接口。也不依赖未公开的 Amp 内部存储格式。
- Amp adapter 在执行时，将公开 user/assistant/tool 事件映射成既有 `HistoryItem`，
  在 worker root 的专用目录追加保存脱敏后的 JSONL；同一 session 的修订追加到同一文件。
  reasoning、认证/配置和统计事件不进入历史；最终 result 不再重复作为一条 assistant 消息。
- `WorkerSession.read()` 按 ID 格式选择 Codex 原生历史或 Amp 本地记录。
  共用必要的安全打开、分页预算、文本截断与脱敏规则，格式映射分别保留；
  不伪造 Codex rollout 格式，不复制整套文件安全逻辑。
- 保持 cursor 与 session、文件身份及记录位置绑定；防符号链接逃逸，完整行才可消费，
  不因取消丢弃此前已写入记录。Amp 历史仅覆盖 worker 实际捕获的运行，
  不承诺导入手工继续或此功能启用前的 thread 内容。
  文件与 worker root 一起备份；本 PR 不新增自动删除或远程归档策略。

#### 部署与文档

- `Dockerfile` 在 worker target 安装可固定版本的 Amp CLI；serve target 不包含它。
  实施时核实官方安装产物、Linux amd64 支持和校验方式，禁止镜像构建时下载 latest 后漂移。
- 更新 worker 部署样例与原生/Docker/Kubernetes 说明：后端选择、`AMP_API_KEY`、
  专用 HOME/settings、持久数据、超时及安全隔离；不修改真实部署或 secret。
- 扩充既有镜像 smoke，检查两个 CLI 可启动及角色边界，明确此检查不代表真实模型验收。
  更新计划索引、README 和架构文档时保留工作区已有的控制台、发布流程改动。

### 备选方案与取舍

- **只添加 `amp -x` 调用：** 改动较少，但修订恢复、结果校验和会话查询不完整，不采用。
- **引入 Amp Python SDK：** 可省部分命令构造，但仍需控制进程组、凭据与持久历史；
  本项目已有 CLI adapter，直接适配可减少依赖和调用层。
- **每次查询导出整个 thread：** 无需保存本地记录，但会话越长查询成本越高，
  且查询依赖 Amp 网络；采用执行时捕获公开流，代价是新增 worker 本地历史文件。
- **通用 agent 框架或数据库改名：** 当前两个 adapter 已能通过现有端口装配，
  不引入这些无验收必要性的改动。

### 风险与限制

Amp 的 thread、账户权限及服务端工具与 Codex 本地 rollout 不同；专用账户/环境和有效工具
集合验证是接入前提。JSON 提示可能不被模型遵守，届时按结果校验失败处理。
CLI 升级可能改变错误和输出格式，因此固定部署版本，未确认执行状态时绝不自动重试。
新增本地历史有磁盘成本；仅保存公开、截断、脱敏后的记录，保留现有单页/扫描上限。
不把自动化替身测试称为真实 Amp/GitHub 闭环验收。

### 实施顺序

1. 校准固定版本 CLI 的事件、MCP 设置优先级、恢复拒绝与安装方式，保存脱敏契约样例。
2. 实现 Amp adapter、配置装配与代表性测试，沿用原 executor 完成首轮和修订核验。
3. 扩展 session ID 契约和有界历史读取，验证 control → worker 查询与恢复/取消。
4. 更新部署、镜像 smoke 和当前文档，运行回归、静态检查及构建；在同一 feature record
   追加实际实施和验证证据。未获提交/推送授权时保持本地改动。

### 验证与消融策略

- **Adapter 合同：** 用假 CLI 进程验证 start/resume、真实 ID、最终 result、错误/残缺流、
  schema 提示、cwd、当前 capability 和旧 MCP 禁用。用本地可控子进程验证取消/超时回收；
  I/O、网络和模型测试保持确定性，不为每个 helper 添加独立测试矩阵。
- **执行闭环：** 复用 `test_worker_executor`、`test_revision_delivery`、`test_worker_tools`、
  `test_worker_client`，以 Amp 事件替身覆盖 INITIAL 工具回执及一轮同 session REVISION，
  保留 Git/jj 真仓库核验。确认通用失败不能启动第二次 agent。
- **历史合同：** 验证 `T-UUID` 贯穿 conversation API、公开消息/工具映射、修订追加、
  cursor 续读和 secret/思考排除；复用已有预算、符号链接和连接恢复回归。
- **消融：** 完成实现后分别移除一次 REVISION 的 MCP 禁用、最终 result 成功检查、
  Amp ID 支持，确认代表性回归各自失败；恢复后重新执行相关测试。
  不为简单装配分支安排无意义消融。
- **检查：** 相关 pytest 后运行有 PostgreSQL 的全量 pytest、`uv run ruff check app tests`、
  `uv run mypy app tests`、`uv build` 和 `git diff --check`。
  在可用的 Linux amd64 Docker 环境运行 worker 镜像 smoke；环境不足时明确记录未验收项。
- **真实 Amp：** 当前没有执行授权或隔离环境验证，不启动新 agent。
  如需真实模型/恢复/MCP smoke，单独取得明确授权并在用户自有的隔离测试仓库执行；
  对其他人的仓库只准备步骤，由用户手工运行有副作用的操作。

### 开放问题与后续工作

无阻塞用户审阅的产品问题。实施阶段必须确定固定 CLI 的可用安装产物、有效工具限制、
恢复拒绝证据及事件细节；若任何一项不能满足既有安全契约，记录事实并调整计划后再继续。
模型/模式自定义、按 Flow 选择后端、跨后端迁移和 Amp 历史导入暂不纳入本次。

## 2. PR change timeline

### [001] — 核实 Amp 原生历史导出，修正只采集 worker 输出的方案

- **背景：** 用户询问能否从本机 Amp 查到在其他设备手动继续同一线程产生的历史。
  初始计划选择执行时捕获 stream JSON，尚未实测原生历史导出；
  “无法展示外部继续的内容”是该采集方案的限制，不能归因于 Amp 查不到这些内容。
- **已执行的只读核实：** 本机 `~/.local/share/amp/` 没有 `threads/` 目录；
  `history.jsonl` 有 11 行，末条记录仅含 `cwd`、`text` 两个字段。
  `runner/` 在本次检查深度内只发现一个 runner JSON 文件。
  这些观察没有证明完整历史以公开格式落盘，也没有证明其他版本或环境不存在本地副本。
- **原生导出证据：** 对[当前讨论线程](https://ampcode.com/threads/T-01a155a6-f796-70fc-8121-9f041c0e1edf)
  运行 `amp threads export T-01a155a6-f796-70fc-8121-9f041c0e1edf`，返回 JSON 的 `messages`
  包含最初的适配请求、关于外部继续的追问，以及从另一设备发出的本轮问题。
  最近一次检查时有 39 条消息；仅输出字段、计数和匹配布尔值，没有保存或公开原始 transcript。
  没有启动/继续 agent，也没有发送消息或修改线程。
- **官方契约：** [Threads 文档](https://ampcode.com/docs/threads#export-a-thread) 将
  `threads export` 定义为与 Web 的 full JSON Export 相同；同一 thread 可跨 Web、CLI、App 访问。
  [Cross-Client Access](https://ampcode.com/docs/cli/remote-control) 明确支持从 Web 继续 CLI thread。
  当前帮助未提供 export 分页/增量参数；`threads raw` 标为仅内部用户可用，不作为产品依赖。
  尚未验证 export 是否联网、使用哪种缓存，不能声称它支持纯本地离线读取。
- **提议修正，待批准：** 替代初始计划“会话读取”中仅依赖 worker 录制的方案，
  改为按需通过公开 `threads export` 获取原生 thread 快照，再映射到现有会话 API。
  外部继续后已进入原生 thread 的公开消息也在展示范围内；不保留独立录制历史作为第二套来源。
  在 worker 内对快照做必要的脱敏缓存与分页，并限制导出超时、总字节、并发及刷新频率。
  导出失败或超预算通过既有错误报告，不把不完整快照冒充完整历史。
  不通过读取未公开磁盘格式或调用 internal-only 的 raw 命令绕开限制。
- **实施前还需校准：** 普通 export 的消息身份、顺序与 compaction 后覆盖范围，
  外部追加/编辑时的缓存刷新和 cursor 失效策略，以及专用 `AMP_API_KEY` 的导出权限。
  若公开导出不足以保留稳定分页或原始历史，先记录事实并调整方案，不宣称已经支持。
- **取舍与验证：** 相较执行时录制，原生导出能够读取本轮实测的跨设备消息，减少第二套历史记录；
  代价是全量快照的读取成本以及尚未确认的网络依赖。
  拟新增代表性验证：worker 执行结束后原生 thread 追加公开消息，后续查询能够刷新并返回，
  同时保持凭据/思考过滤、预算和分页一致性。当前仅修改计划，尚未实现或执行这些测试。

### [002] — 支持 CLI 原生登录，纠正 API key 必填的假设

- **背景：** 用户指出 Amp 可以直接登录。初始计划和此前答复将 `AMP_API_KEY` 视为 worker
  必填配置，未考虑 CLI 保存的登录账户，这一限制没有依据。
- **依据与实测：** [官方账户说明](https://ampcode.com/docs/cli#manage-cli-accounts) 明确
  `amp login` 保存并激活账户，后续 CLI 命令使用该账户；如果设置了 `AMP_API_KEY`，环境变量
  优先于保存的账户。本机执行 `env -u AMP_API_KEY amp threads export` 加当前线程 ID，
  成功解析对应线程及 55 条消息；未读取或输出凭据，没有启动/继续 agent。
  这验证原生登录可用于当前账户的只读导出，不等同于执行和长期续期已验收。
- **提议修正：** 替代初始计划的 API key 必填约束，沿用 Amp CLI 原生认证选择：
  用户可在 worker 的运行身份与环境下先执行 `amp login`，worker 使用 CLI 保存的登录态；
  `AMP_API_KEY` 作为可选认证方式，设置时仍按 CLI 自身优先级处理。
  adapter 不解析凭据文件、不把登录 session token 转存为环境变量、不添加自定义认证模式。
- **安全与验证：** 保留专用 worker 的隔离要求，不挂载整个个人 HOME 或复制个人 secret；
  容器部署需让该 worker 的 CLI 凭据存储按原生机制持久化。缺少有效认证时报告失败，
  不在后台 worker 发起交互式登录。后续覆盖无 API key 的已登录运行及可选 API key 路径，
  实测续期、执行和历史读取；当前只修正计划，尚未修改实现或测试。

### [003] — 核实结构化输出文档，区分主 agent 结果与插件 AI 对象生成

- **触发：** 用户要求核实 Amp 文档能否提供结构化结果，重新检查 CLI、Python/TypeScript SDK、
  插件 API 和 MCP 文档，不能仅根据 CLI 没有 `--output-schema` 推断 Amp 全部能力。
- **CLI/SDK 已确认：** [stream JSON schema](https://ampcode.com/docs/cli/streaming-json#message-schema)
  定义成功事件的 `result` 为 `string`；
  [Python AmpOptions](https://ampcode.com/docs/sdk/python#ampoptions) 和
  [TypeScript AmpOptions](https://ampcode.com/docs/sdk/typescript#ampoptions) 未列出用于主编码
  agent 最终回答的 output schema/response format 设置。文档中的 structured stream
  指 system/assistant/result 事件封装，不能据此声称业务 JSON 已受 Schema 约束。
- **插件原生能力：** [Plugin API](https://ampcode.com/docs/plugin-api) 的
  `PluginAIGenerateObjectRequest` 接收 `prompt` 与 `schema: PluginAIObjectSchema`；
  `amp.ai.generate<T>(request)` 返回 `Promise<T>`，文档明确说明有 schema 时返回已校验对象。
  这是一次单独的 AI 请求，按当前 thread 路由和计费，不是主编码 turn 的输出格式开关；
  不能假定它自动获得完整编码上下文，也不能用二次生成替代 VCS 和交付事实核验。
- **另一候选：** 通过带 `inputSchema` 的插件或 MCP 结果提交工具，让编码 agent 提交
  INITIAL/REVISION 对象，由 worker 使用既有 Pydantic 模型强校验后接收。
  本项目已有带 input schema 的 [WorkerMCP](../../app/worker/transport/mcp.py)，
  [插件工具定义](https://ampcode.com/docs/plugin-api) 也公开支持 JSON Schema 输入。
  这可以避免依赖最终自然语言回答或增加一次模型整理调用，但不能保证模型一定调用工具、
  工具参数一定有效或业务事实正确；缺少有效提交仍须失败。
- **设计状态：** 初始计划“仅 schema 提示加结果校验”不再视为唯一可行方案。
  本次只核实文档并记录候选，尚未选定或实现插件/结果工具方案；采用工具提交会涉及
  每轮 MCP 工具权限与结果接收设计，须明确修订时只暴露新一轮所需工具，绝不复用旧 broker。
  当前没有运行模型、安装插件或修改实现；文档能力不等同于 worker 集成已通过验证。

### [004] — 2026-10-21：实现本地 Amp 后端，按实测收窄恢复与取消保证

- **背景与决定：** 用户批准实现；采用 [001] 的原生 export 与 [002] 的 CLI 原生认证修正。
  对 [003] 的候选，当前选择 Schema 提示加原始最终文本，再由既有 Pydantic/VCS/交付回执核验；
  不新增插件、SDK、结果提交工具或二次模型整理，避免引入新的结果权威和每轮 MCP 权限。
- **实现：** `amp.py` 实现 start/resume，`amp_cli.py` 统一临时 settings、凭据环境和有界进程监督。
  prompt 经 stdin；固定 private/local/low、不自动 archive；只接收匹配 init/result 与明确成功终态。
  `runtime.py` 按 worker 配置选择后端及 revision deadline，Codex 默认保留。复用已存在的
  Git helper、临时 gh 包装和单轮 GitHub capability，不改个人 Git/gh 配置；ARCHIVE 复用 workspace
  释放流程，不远程归档 Amp thread。提交作者继续由 Git 配置/作者环境变量决定。
- **恢复边界：** 真实缺失 thread 只有一个固定 stderr 样例，尚不足以排除所有鉴权/网络情况。
  因而 supersede 初始计划中“已保存 thread 不存在时自动 fallback”的条件分支：Amp 不抛
  `SessionUnavailable`，未知 resume 一律失败、不重放；任务原本无 session 的单次 start fallback 不变。
- **取消边界：** 实测直接 SIGKILL CLI 进程组会遗留另一个 PGID 的 native shell；SIGTERM/SIGINT
  分别清掉一个实测 shell，但 CLI 退出 0、result 为 error。因此先取子进程组快照、SIGTERM CLI、
  有界等待后升级清理捕获组，最后才传播取消；`is_error/subtype` 必须参与成功判定。
  这收窄初始“回收所有子进程”的保证：快照有竞态，不保证任意脱离进程树/双重 fork 的守护进程；
  长期 worker 容器不按轮销毁，外部隔离仍为前提。Linux native 行为尚未实测。
- **设置优先级：** [官方 settings 文档](https://ampcode.com/docs/cli/settings) 明确 workspace
  覆盖 `--settings-file` 对应的用户设置，managed policy 还会覆盖或合并。执行前发现 workspace
  `.amp/settings.json[c]` 即拒绝，不删除/改写；MCP 按本轮 URL allow、其余 command/URL reject，
  REVISION 禁用旧 flow。有效工具列表用于结果拒收，不能证明额外工具从未运行；managed
  policy、账户插件与服务端 MCP 必须由运维核查，不将设置当成 OS/权限隔离。
- **原生历史：** session ID 共用受限 UUID/`T-UUID` 类型，protocol 在 bootstrap v14 基线上递增
  为 v15，journal 仍 v4。`AmpHistory` 对公开块分页，ID 带 session 前缀，cursor 校验已消费公开
  前缀；追加可续读、编辑失效。只缓存已脱敏公开投影，排除 thinking；最多 16 个快照、两秒 TTL，
  两个并发 export、20 秒/2 MiB 导出上限、500 项/256 KiB 页面上限。短快照也按原生结果处理，
  避免把真实删除误判为复制滞后而永久展示旧尾部。
- **集成修正：** control 的 broker 要求每条历史记录 ID 以 session 开头。新增 Amp HTTP→WS→worker
  场景最初返回 `invalid_response`，据此补上原生消息 ID 外的 session 前缀；块内分页避免单条
  native 消息超过 limit 时原地循环。两处均由现有公开 API 场景验证，不放宽 control 校验。
- **部署与发布次序：** worker 镜像固定 Amp `0.0.1792555249-g702cd1`，校验官方 Linux x64
  解压产物 SHA-256 `41a2e3680c63091d7c0293e2ad702af95772f9f35d977b74c7794fa40e45b5c9`；
  增加 procps 供进程快照使用，serve 不安装 Amp。与 bootstrap 线程协调其先独立交付 v14，
  不携带未完成 Amp hunks；本功能稍后独立交付 v15，目前没有发布授权。

### [005] — 2026-10-21：本地实测与机制消融

- **真实 CLI：** 通过实际 `AmpAgent` 在全新临时 Git 仓库执行 INITIAL 提交并推送到本地 bare，
  真实 WorkerMCP 核验后调用替代 control RPC、返回 `prepared`；继续同一 thread 完成第二次
  commit/push，原始结果分别通过 InitialCodingOutput / RevisionCodingOutput，VCS 核验通过，
  作者保持 `Worker Adapter Probe <probe@localhost.invalid>`。原生 export 读到 12 个公开历史项、
  包含工具结果。环境无 `AMP_API_KEY`，沿用已登录 CLI；未向 GitHub 写入、未创建真实 PR。
  证据为本地 `adapter-smoke.json`，不把替代 RPC 验证称为完整线上 GitHub 闭环。
- **消融：** 在独立 Python 进程修改函数定义，不改共享工作区：移除终态错误判定，代表性取消
  测试将错误结果当成功；禁用 flow 的标志改为 false，当前轮能力合同测试失败；移除子进程组快照，
  独立 PGID 子进程存活，监督器回归失败。三项均得到预期 pytest exit 1；子进程由测试 finally
  清理。第三项替代初始计划的简单 ID 支持消融，优先验证实测发现的独立进程组风险；ID 由完整
  HTTP/WS 场景覆盖。所有机制保留，正常实现重跑通过。
- **证据位置：** 调研报告、`adapter-smoke.json`、`ablations.log` 保存在
  `.amp/in/artifacts/amp-cli-calibration-20261021/`；本次临时验证/消融脚本已删除，
  保留日志与结果，这些本地材料不随代码提交。实际模型探测仅限临时本地 Git/bare，
  没有覆盖 Linux、真实 App token 鉴权或 Docker 构建。本机无 Docker，不能宣称镜像 smoke 通过。
- **最终验证：** 在当前共享工作区（包含其他功能未提交改动）使用独立 PostgreSQL 17 实例运行
  `FLOWTOOL_TEST_DATABASE_URL=postgresql://b1indsight@127.0.0.1:55477/postgres .venv/bin/pytest -q`：
  **969 passed**，一个既有 Starlette/httpx 弃用警告；日志 `pytest-final.log`。Ruff 全量通过，
  mypy 211 个源文件无错误，`uv build` 产出 wheel/sdist，`bash -n scripts/smoke/docker.sh` 与
  `git diff --check` 通过；mypy/构建日志在同一证据目录。PostgreSQL 测试 schema 已清空、独立实例
  已停止。没有 UI 外观改动，Amp 会话经真实 HTTP/WS handler 的集成测试覆盖。
- **独立交付复验：** 从 PR #103 的独立 protocol v14 / journal v4 实现快照建立专用 jj workspace，
  只带入 28 个 Amp 相关文件，不包含共享工作区的其他未交付功能。该独立快照相关测试
  **49 passed**；带 PostgreSQL 的完整回归 **920 passed**，一个既有弃用警告，日志
  `pytest-pr.log`。Ruff、mypy（204 个源文件）、wheel/sdist 构建和 Docker smoke 的 Shell
  语法检查通过；测试 schema 为 0，专用 PostgreSQL 实例已停止。该 PR 依赖 #103 先发布，
  共享工作区的 969 项结果保留作集成证据，不冒充独立快照的测试数量。

### [006] — 2026-10-21：修复首轮审查报告的四项 Amp 问题

- **JSONL：** 仅按 LF 字节切分记录，保留 JSON 字符串内合法的 U+0085/U+2028/U+2029，
  不改变完整 init/result、同 session 与错误终态检查。
- **脱敏与诊断：** 将 Amp 历史已有的通用 token/运行时凭据过滤集中到 Amp CLI 边界，
  新增官方 `sgamp_` 访问 token 前缀；历史公开块、最终答案和失败诊断复用同一规则。
  初始化前失败保留 stderr，先脱敏再限至 4000 字符；保持未知执行不自动重跑。
- **时限取舍：** 保留原生导出 20 秒，将 control broker 的单次查询窗口统一从 10 秒调整为
  30 秒，覆盖最长约 9 秒的进程清理；避免新增按后端分支的超时设置。控制台同步已有外层
  30 秒时限，HTTP 仅在客户端断开时取消；既有超时/取消清理与并发限额不变。
- **回归证据：** 两项新测试及两项既有场景扩展，在修复前得到 **4 failed**，修复后同组
  **4 passed**（`amp-fixes-before.log` / `amp-fixes-after.log`）。慢导出通过实际 HTTP → WS →
  Worker → AmpHistory 链路，以 1:10 缩时验证 15 秒等效导出超过旧 10 秒窗口仍可返回，
  不是只断言配置值。其余覆盖三类公开块的外部客户端 token、Unicode 答案及脱敏有界诊断。
- **当前快照验证：** 在原 v14 基线上带 PostgreSQL 复验 **923 passed**，一个既有弃用警告，
  日志 `pytest-fixes.log`。Ruff、mypy（204 个源文件）、wheel/sdist 构建、Shell 语法及差异
  检查通过；测试 schema 已清空，专用 PostgreSQL 已停止。尚未整合 #103 后续修复，
  不沿用旧 head 的绿色 CI 作为本轮修复结果；后续审查与发布仍待完成。
- **依赖：** #103 的五项问题由原负责线程在独立 workspace 修复，避免并发改动相同文件；
  取消后数据保留策略尚待确认，不把本轮 Amp 修复表述为已完成全部依赖修复。

### [007] — 2026-10-21：将 Amp 实现及修复重放到已合并的 main

- **基线变化：** #103 已合并为
  [`b22b862c2773`](https://github.com/b1indsight/agentflow/commit/b22b862c2773e5860abb538ebfa44d5627ca547a)。
  在专用 `amp-pr` workspace 将原 Amp 实现及 [006] 修复重放到该 main，无冲突；保留 main
  的标签初始化等已合并功能，不切换或重置共享工作区。protocol 仍为 v15，journal 仍为 v4。
- **复验：** 独立 PostgreSQL 全量 **935 passed**，一个既有 Starlette/httpx 弃用警告；
  Ruff、mypy（206 个源文件）、wheel/sdist 构建、Shell 语法及差异检查通过。日志为同一
  本地证据目录的 `pytest-rebase-main.log`、`ruff-rebase-main.log`、`mypy-rebase-main.log`
  与 `build-rebase-main.log`；测试 schema 为 0，专用数据库已停止。
- **未纳入范围：** 不把 bootstrap 线程尚未发布的四项 medium 修复当成 main 内容，也未
  整合其未完成的取消回收修复。取消快照保留或删除策略仍待用户确认，不清理 dirty/untracked
  工作区，不修改 merged Flow 的 ARCHIVE 绑定。
- **交付边界：** 本次重放仅本地完成；#107 的远端 head 与 base 尚未更新，本轮后续审查未运行，
  未推送、合并或部署，不将旧 head 的 CI 结果用于这份快照。

### [008] — 2026-10-21：接入取消回收合同，Amp 协议递增为 v16

- **基线：** 将 Amp 实现及 [006] 修复重放到 [PR #109](https://github.com/b1indsight/agentflow/pull/109)
  的固定 head [`fdd08abea14c`](https://github.com/b1indsight/agentflow/commit/fdd08abea14ce3a70c912904eee21810ba478d6d)。
  该 PR 保留四项 workspace/VCS 修复，并完成取消回收；不同于已合并的 #103，尚未合并 main。
  只在 Amp 专用 workspace 整合，保留双方 CHANGELOG 与 README 的冲突内容，未修改共享树。
- **协议决定：** 取消回收独立使用 wire v15 / journal v5 / manifest v2；Amp 的会话 ID 扩展
  单独递增到 wire v16，继承新的 journal/manifest，不复用两种不同的 v15 消息合同。
  同步修改协议断言、部署、升级和取消说明；旧 v15 消息按现有严格解析规则拒绝。
- **取消语义：** 继承用户明确要求的取消回收策略：执行停止并取得持久授权后，删除准确归属
  的取消 workspace，含未提交、未跟踪文件与缓存，不额外保存快照；落盘后发送 StopCompleted。
  清理失败仍阻塞分配，已知 merge 保留原目录归档。Amp 的历史读取与新增清理 journal 同时保留。
- **组合验证：** PostgreSQL 全量 **957 passed**，一个既有弃用警告；Ruff、mypy（207 个源文件）、
  wheel/sdist 构建、Shell 语法与差异检查通过。额外从公开解析入口验证 register、停止授权与
  清理回执的 v16 round-trip、拒绝对应 v15 消息，并确认 journal v5 / manifest v2。
  证据为 `pytest-cancel-v16.log`、`ruff-cancel-v16.log`、`mypy-cancel-v16.log`、
  `build-cancel-v16.log` 与 `protocol-cancel-v16.log`；测试 schema 为 0，专用 PostgreSQL 已停止。
- **保留的依赖风险：** #109 的后续审查无 critical/high，但另报两个 medium，本次不擅自修复：
  取消 overflow 后，同 Flow 重试改用另一 idle copy 时，旧 absent 条目可能干扰 lookup；
  jj seed 对 `main;feature` 等 Git 合法分支名仍直接按 revset 解析并失败。
  取消入口、权限与状态优化继续由 [Issue #108](https://github.com/b1indsight/agentflow/issues/108) 跟踪。
- **发布边界：** 合并顺序为 #109 → #107；保持原 Amp bookmark/PR，不新建第二个实现 PR。
  本次组合验证不代替新 head CI，也不扩大 Docker/Linux、真实 installation-token 写入验收范围。

### [009] — 2026-10-21：跟随 #109 收窄取消回收，不增加二阶段清理合同

- **取代 [008] 的基线和取消方案：** 用户要求 #109 只保留取消 workspace 回收，已撤出四项额外
  workspace/VCS 修复、StopCompleted、清理决定数据库列和 journal/manifest 升级。Amp 在独立
  workspace 重放到其新固定 head
  [`cf718394272f`](https://github.com/b1indsight/agentflow/commit/cf718394272f854cb8fdccfdb10a1c71c13fe2d2)，
  未将共享树其他功能或已撤出的修复带回。相对该 head 的 gateway/client、workspace/VCS、
  数据库及 journal 文件管理实现没有增量；Amp 仅在 WorkerSession 增加历史来源选择。
- **当前合同：** wire 仍递增为 v16，沿用 journal v4 / manifest v1 和原数据库 schema。
  停止保持 StopExecution → ExecutionStopped → StopAccepted，worker 依据 ACK 的
  `release_workspace` 区分取消删除与 merge 保留；删除成功后才持久化 journal ACK 并读取
  下一任务。控制面仍在原停止确认时释放 binding，`stop_pending=False` 不证明磁盘删除完成。
  不增加第二次完成握手、持久清理决定、inode 协议或通用部分删除恢复。
- **复验：** 独立 PostgreSQL 全量 **938 passed**，一个既有 Starlette/httpx 弃用警告；Ruff、
  mypy（206 个源文件）、wheel/sdist 构建、Shell 语法与差异检查通过。额外从公开解析入口
  验证 register 及 `release_workspace=True/False` 的 v16 round-trip，拒绝对应 v15 消息，
  确认 journal v4 / manifest v1；这项临时解析验证不代替 merge 保留的永久端到端回归。
  日志为 `pytest-cancel-minimal.log`、`ruff-cancel-minimal.log`、`mypy-cancel-minimal.log`、
  `build-cancel-minimal.log` 与 `protocol-cancel-minimal.log`。测试 schema 为 0，专用数据库
  已停止。957 项属于 [008] 的旧组合；本次少 19 项来自依赖撤出的修复及专属测试。
- **依赖风险：** #109 新后续审查披露 **1 high / 1 low**，不与 [008] 旧基线的两个 medium
  混用。High 是旧 workspace 清理失败后携带旧 stop journal 重连，gateway 跳过下一条已派发
  Flow 的 assignment 对账，重试清理成功后任务仍可能 RUNNING/未 Started，worker 等待任务；
  原负责线程用真实 PostgreSQL 及实际 gateway/client 独立复现。Low 是缺少永久的
  `release_workspace=False` 端到端保留测试。两项均未在 Amp 增量中修复，目前不建议合并。
- **Amp 保留风险：** [008] 对应 Amp 修复的后续独立审查为 **0 critical/high、1 medium、1 low**。
  Medium 是工具载荷先 JSON 编码，换行后的外部 token 可能绕过词边界过滤；Low 是 INITIAL
  executor 未把 adapter 的启动诊断传到控制面。按约定仅报告，本次基线重放不追加修复。
  发布前另行检查本次基线整合；不把旧 head 的绿色 CI 或无检查结果当作新组合通过。
- **交付边界：** 保持同一 bookmark 和 Draft PR #107，依赖顺序仍为 #109 → #107；未合并或部署。
  取消入口、权限和状态优化仍由 [#108](https://github.com/b1indsight/agentflow/issues/108) 跟踪。
  Docker/Linux 与真实 installation-token GitHub 写入验收仍未完成。
