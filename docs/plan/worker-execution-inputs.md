# Worker 执行输入的职责边界 — Feature record

**Stage:** Planning — 待审阅，尚未实施。
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
