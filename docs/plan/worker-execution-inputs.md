# Worker 执行输入的职责边界 — Feature record

**Stage:** Implemented — 本地验证完成，待代码审阅。
**PR:** [#70](https://github.com/b1indsight/agentflow/pull/70)。
**Bookmark:** `refactor/worker-execution-inputs`
**基线:** `main` / `a7c91202`（PR #68），2026-10-10。

## 1. Initial plan

### 背景、目标与范围

`CodingExecutionContext` 同时保存 cwd、push 凭据和 MCP 配置。start 从独立参数获取
cwd，resume 从 context.cwd 获取；`_start_args()` 又以 cwd 是否存在决定关闭 flow MCP。
VCS 的 push_context 接收整个编码上下文，却只使用 push_token。push_repository 在
生产代码中只有赋值，没有读取。
依据：[上下文](../../app/domain/models.py)、[adapter](../../app/worker/execution/codex.py)、
[executor](../../app/worker/execution/executor.py)、[VCS](../../app/worker/execution/vcs.py)。

目标是目录、凭据、工具配置分别由其实际消费者接收；让目录不再隐含代表运行模式。
范围限于 CodingAgent 端口、Codex adapter、executor、凭据 helper 和 VCS 调用链。
HTTP、worker wire、AssignmentSecret 和 PushSecret 的结构不变。

### 拟议行为与接口

start/resume 均显式接收当前 Flow 的 cwd；resume 不能从保存的 Codex 会话配置猜测目录。
`CodingExecutionContext` 保留短期 push_token 与 MCP 配置，删除 cwd 和未消费的
push_repository。无需为每个字段添加新的包装类型。

示意接口：`start(task, cwd, *, output_schema, context)` 与
`resume(session_id, prompt, *, cwd, output_schema, context)`。
cwd 是必需输入；CLI cwd 与子进程 cwd 均从这一个值生成。

MCP 配置由是否显式提供当前运行的 MCP endpoint 决定。INITIAL 提供当前 WorkerMCP；
REVISION resume/fallback 均显式关闭保存的 flow broker；未提供 endpoint 时关闭 flow
MCP，不能根据 cwd 分支。endpoint/token 成对校验继续保留。
VCS 只接收短期 push token，既不接收目录配置也不接收 MCP secret。

### 技术方案与模块

- `app/domain/models.py`、`ports.py`：收窄 CodingExecutionContext；统一 CodingAgent 目录输入。
  VcsExecutionContext 的 push_context 改为实际使用的 push_token。
- `worker/execution/executor.py`：在 INITIAL/REVISION/start/resume/fallback 明确传入 workspace；
  AssignmentSecret 的 repository、过期时间及 worker binding 校验仍在 executor 完成。
- `worker/execution/codex.py`：统一 CLI/进程目录，按显式 MCP 配置生成参数，保持输出解析、
  deadline、取消进程组及 proven non-execution 的 SessionUnavailable 分类。
- `credentials.py`、`vcs.py`：凭据 helper 接收 `SecretStr | None`；远端核验只传 token，
  继续在 agent-controlled 仓库之外验证固定 URL，不持久化或记录 secret。
- 同步所有 CodingAgent 测试替身，更新必要的架构接口说明。

### 备选方案与取舍

仅删除 push_repository 收益有限，仍保留目录双入口及隐式 MCP 模式。
拆成三套 context/spec 并反复转换会增加中间类型；本方案复用现有 capability 容器，
目录用必需参数、VCS 用 token，避免新的转换链。

### 风险与约束

resume CLI 与 start CLI 的参数能力不同，仍使用现有适配方式，不改 CLI 版本。
显式关闭 flow MCP 必须覆盖 fallback，不能误连 INITIAL 保存的 broker。
push_token 仍为 SecretStr，调用参数、环境构造、返回值和日志不得泄漏；仓库身份验证
不能因删除未使用的 push_repository 而被删除。

### 实施顺序

1. 调整代表性 start/resume/fallback 测试，固定单一 cwd 来源与 MCP 开关行为。
2. 修改端口、context、adapter 和所有调用者，再收窄凭据/VCS 输入。
3. 删除失效字段和分支，检查真实 Git/jj 回归及文档。

### 验证与消融

复用 `test_codex_driver`、`test_worker_executor`、`test_revision_delivery`、`test_vcs`、
`test_worker_tools` 和 `test_worker_client`。验证恢复目录、fallback、过期/错误仓库 capability、
凭据隔离和 MCP token 脱敏；避免每个调用点重复同一参数断言。
实现后移除一次 revision 的 flow MCP 禁用配置，确认代表性回归捕获旧 broker 复用；
移除一次 resume cwd 设置，确认目录回归捕获遗漏。若现有测试只能检查命令形状，先明确
这种证据范围，不把它描述为真实 Codex 执行验证。消融后恢复代码，再运行相关检查。
最终运行真实 PostgreSQL 全套、Ruff、Mypy、`uv build` 和 diff 检查。

### 开放问题与后续工作

无阻塞设计问题；具体参数排布在实现时按端口约定确定。其他 agent adapter、新 capability
模型和通用运行模式枚举不纳入本 PR。与另外三个计划无实现依赖。

## 2. PR change timeline


### 2026-10-10 — 执行输入与 capability 消费边界

- **背景与实现**：start/resume 均以必需 cwd 参数设置 CLI 和子进程目录；executor 在
  INITIAL、REVISION resume/fallback 显式传入 Flow workspace。删除 context.cwd 与
  push_repository；repository、有效期、worker binding 校验仍保留在 executor。
- **决定与取舍**：Codex 的 `_mcp_args()` 统一当前 endpoint/token 的成对校验、启用与
  禁用参数，start/resume 共用同一规则，避免两处配置发生漂移。未提供 endpoint 时总是
  覆盖保存的 flow MCP 为 disabled；修订不提供 MCP capability，fallback 同样关闭。
  不增加新的 context/spec 类型或运行模式枚举。
- **凭据边界**：VcsExecutionContext、prepare_revision_context、远端 SHA 查询和 credential
  helper 只接收 SecretStr push_token；仍在 agent 仓库之外使用隔离环境查询固定 URL。
  增加代表性回归，检查 fresh token 的环境注入、worker/MCP secret 隔离与固定 argv。
- **版本基线**：在同一 bookmark/PR 上更新到 main `52d493d6`（已合并 #69），无新增堆叠依赖。
  同步架构接口说明、changelog 与 CodingAgent 替身；wire、HTTP 和持久化结构保持不变。

### 2026-10-10 — 消融与验证

- **测试先行**：目录/MCP 回归在旧实现上确认失败：start 子进程 cwd 为 None，resume
  不接受显式 cwd。完成实现后计划指定的六个测试模块通过 91 项。
- **消融设置**：每次只修改 `app/worker/execution/codex.py` 的一项配置；统一执行
  `uv run pytest tests/test_codex_driver.py -q --tb=short -k run_uses_explicit_directory_and_current_mcp`。
  本地复现脚本：`.cache/2026-10-10/pr70-ablation.py`，通过 finally 恢复原实现。

| 移除的机制 | 观察结果 | 决定 |
| --- | --- | --- |
| `_mcp_args()` 无 endpoint 分支改为返回空列表 | 2 failed / 2 passed，start/resume 未关闭旧 broker | 保留显式禁用 |
| resume 的 `-c cwd=...` 配置 | 2 failed / 2 passed，resume 缺少当前目录配置 | 保留 CLI cwd 覆盖 |
| 子进程 `cwd=cwd` 参数 | 4 failed，start/resume 进程目录缺失 | 保留统一进程目录 |

- **证据范围**：消融检查生成的 CLI 参数及 subprocess cwd/environment，证明回归能捕获
  配置遗漏；没有执行真实 Codex 或连接保存的 broker，不能据此声称真实恢复已验证。
- **恢复后验证**：`uv run pytest tests/test_codex_driver.py tests/test_vcs.py -q --tb=short`：
  32 passed（包括新增的 VCS token 隔离回归）。
- **全量验证**：`FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool@127.0.0.1:55419/postgres uv run pytest -q --tb=short`：
  843 passed、0 skipped，59.42 秒；全量收集之后新增的一个 VCS 回归由上述 focused run 验证。
  一条既有 Starlette/httpx 弃用警告。
- **检查与构建**：`uv run ruff check app tests`、`uv run mypy app tests`（160 source files）、
  `uv build`、`git diff --check` 均通过。真实 Codex 多轮执行仍未验证，不扩展本 PR 验收结论。


### 2026-10-10 — 精简 Codex adapter 回归测试

- **触发与范围**：用户指出 `tests/test_codex_driver.py` 测试偏重；本轮仅调整测试，
  删除 start/resume × 有无 MCP 的组合矩阵，以及重复的凭据环境/argv 断言。
  endpoint 配置与 secret 隔离继续由已有专门测试覆盖。
- **实现与取舍**：保留一个 resume 的 CLI/进程目录与旧 broker 禁用回归，使用 AsyncMock
  代替本地 Process/spawn 类；start 的进程目录断言放入现有输出转发测试，默认 MCP
  禁用仍由已有 start 参数测试覆盖。endpoint/token 成对校验改为两项同步参数测试，
  不再重复通过两个异步入口验证相同 helper。文件净减少 30 行，用例从 19 项减至 14 项。
- **消融复验**：脚本 `.cache/2026-10-10/pr70-test-trim-ablation.py` 每次移除一项机制，执行
  `uv run pytest tests/test_codex_driver.py -q --tb=short -k 'trusted_host_start or adapter_returns_raw or resume_uses_current_directory'`。
  移除 MCP 禁用：2 failed / 1 passed；移除 resume CLI cwd：1 failed / 2 passed；
  移除进程 cwd：2 failed / 1 passed。恢复后关键配置遗漏仍能被精简后的测试发现；
  证据范围保持为命令/进程参数，不是实际 Codex 执行。
- **验证**：`uv run pytest tests/test_codex_driver.py -q --tb=short`：14 passed。
  六个计划指定模块：`uv run pytest tests/test_codex_driver.py tests/test_worker_executor.py tests/test_revision_delivery.py tests/test_vcs.py tests/test_worker_tools.py tests/test_worker_client.py -q --tb=short`：
  87 passed。`uv run ruff check app tests`、`uv run mypy app tests`、`git diff --check` 均通过。
