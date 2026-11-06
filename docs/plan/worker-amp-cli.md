# Worker Amp CLI 适配

**状态：** 已实现，未部署；PR 与检查状态以 GitHub 为准。
**PR：** [#107](https://github.com/b1indsight/agentflow/pull/107)，目标 `main`。
**基线：** [#103](https://github.com/b1indsight/agentflow/pull/103) 与
[#109](https://github.com/b1indsight/agentflow/pull/109) 均已合并。
**Bookmark：** `feat/worker-amp-cli`。

**后续变更：** [worker 继承现有 skills 与 MCP](#后续变更记录) 已实现并完成本地验证，
沿用 [#128](https://github.com/b1indsight/agentflow/pull/128)，未部署；具体证据和验收限制见 [002]。
上面的 PR 及下文既有证据属于原 Amp 适配；[001] 保留为后续变更的初始方案。

**当前变更：** [003] 的空闲超时与失败诊断方案已按 [004] 实现，沿用
[#131](https://github.com/b1indsight/agentflow/pull/131)，尚未部署。原提案保留为历史，
实际实现、验证和限制见 [004]。

## 目标与范围

为 worker 增加可选 Amp CLI 后端，沿用现有 `CodingAgent.start/resume` 和
INITIAL → REVISION → ARCHIVE 流程。默认仍为 Codex；一个 worker 固定一个后端，
更换前须收尾存量 Flow。不增加 SDK、插件、远程 executor、模型路由或跨后端会话迁移，
不改 UI、Judge、数据库字段名及 control 的 GitHub 操作权威。

## 实现与关键决定

| 位置 | 职责 |
| --- | --- |
| [`config.py`](../../app/worker/config.py)、[`runtime.py`](../../app/worker/runtime.py) | 选择后端和对应超时，复用现有 executor |
| [`amp_agent.AmpCodingAgent`](../../app/worker/execution/amp_agent.py) | 本地私有 thread 的 start/resume、Schema 提示与结果适配 |
| [`amp_process.py`](../../app/worker/execution/amp_process.py) | 每轮环境/settings、进程监督及输出脱敏 |
| [`AmpHistory`](../../app/worker/state/amp_history.py)、[`WorkerSession`](../../app/worker/state/session.py) | 原生历史导出、公开块映射、缓存与分页 |
| [`Dockerfile`](../../Dockerfile) | 仅 worker 安装固定版本 Amp，serve 不包含 Amp |

- **结果权威不变。** prompt 经 stdin；只接受同 session 的完整 init/result、进程退出 0
  及成功终态。最终文本经脱敏后交给既有 Pydantic、VCS 和交付回执核验。Schema 仅作提示，
  不修复 JSON、不剥 Markdown、不增加二次模型生成或结果提交工具。
- **恢复保守失败。** REVISION 显式继续原 `T-UUID`，禁用旧 INITIAL MCP。
  缺失 thread 的 stderr 不足以证明未执行，故不自动重放；原本无 session 的单次 start
  fallback 保留，不按最近 thread 猜测或跨后端重建。
- **使用原生历史。** 通过公开 `amp threads export` 读取快照，支持外部客户端追加；
  不保存第二套录制历史，不读取未公开存储格式。只返回公开消息和工具块，排除 thinking；
  缓存、导出并发/超时/字节和页面预算有界，追加可续读，已消费前缀被编辑则 cursor 失效。
  control 查询窗口为 30 秒，覆盖 20 秒导出及进程清理。
- **逐轮凭据。** 支持专用 worker HOME 的原生登录或可选 `AMP_API_KEY`；每轮 Git/gh 使用
  assignment 的 GitHub App capability，提交作者沿用配置。不继承个人 GH/control 凭据，
  不修改个人全局设置。INITIAL 仅开放本轮 MCP，冲突的 workspace Amp 设置在执行前拒绝。
- **取消与隔离。** 有界读取 stdout/stderr；先捕获子进程组，再 TERM、等待并升级清理。
  随后沿用 #109：清理成功才报告停止，copy/worktree 原地复用，已知 merge 保留位置供
  ARCHIVE。settings 不代替 OS 隔离，无法保证清理任意脱离进程树的守护进程。
- **脱敏只做必要工作。** 文本检查 JSON 字符串字面量；结构化工具载荷直接遍历并序列化
  一次，保留转义 token 和孤立代理码点处理。resume 复用共享 SessionId 校验，不重复设置
  已由上游保证的长度/条数限制。

## 配置与发布合同

- `FLOWTOOL_CODING_AGENT=amp`；默认 `codex`。
- `FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS=1200`；Amp 固定 `--mode low`。
- worker 镜像固定 Amp `0.0.1792555249-g702cd1`，下载官方 Linux x64 产物并校验 SHA-256；
  具体值以 Dockerfile 为准，不在计划中重复维护。
- 最终合同为 **wire v15 / journal v4 / manifest v1**；serve/worker 固定同一 commit 或镜像。
  已撤销的 ACK 字段、StopCompleted、wire v16 及存储升级不再保留，也不提供兼容转换。
- 部署与原生登录步骤见 [部署文档](../deployment/README.md)；ARCHIVE 不删除远端 Amp thread。

## 验证与证据

| 范围 | 已有结果 |
| --- | --- |
| Adapter、历史和取消 | 覆盖 start/resume、终态拒收、当前凭据、Unicode/转义脱敏、外部追加/编辑、分页及真实子进程超时/取消 |
| 修复回归 | 关键修复均有修复前失败、修复后通过的对照；最近相关 Amp 套件 42 passed |
| 机制消融 | 移除终态判定、REVISION MCP 禁用、子进程组快照或转义字符串检查，均捕获预期回归 |
| 本地全量 | 对齐 main 后 PostgreSQL **942 passed、0 skipped**；Ruff、mypy（205 文件）、文档链接、脚本语法和差异检查通过；此前 wheel/sdist 及 wheel 隔离导入通过 |
| 最新已完成 CI | [测试/静态检查/构建](https://github.com/b1indsight/agentflow/actions/runs/39757664265)中 pytest 为 929 passed、13 skipped；[镜像及 Docker smoke](https://github.com/b1indsight/agentflow/actions/runs/39757664244)、[恢复](https://github.com/b1indsight/agentflow/actions/runs/39757664336)、[Kubernetes](https://github.com/b1indsight/agentflow/actions/runs/39757664518)通过，共 8 项成功、publish 跳过；对应精简前提交 |
| 真实 CLI | 临时 Git 仓库 INITIAL → 同 thread REVISION，提交并推送本地 bare，真实 WorkerMCP 核验通过；control RPC 使用替代，未执行真实 GitHub 写入 |

最近职责收窄/重命名及删除静态测试两次独立门禁均为 **0 findings**，不代表下列既有问题
已解决。本地日志位于 `.amp/in/artifacts/amp-cli-calibration-20261021/`，不随代码发布。
本次对齐 main 的冲突处理精确复用了已通过 CI 的组合树，业务代码和运行配置未变化；
新 head 的检查独立追踪，不能用旧 head 的 CI 状态代替。本次本地日志为 `ready-pytest.log`、
`ready-collect.log` 和 `ready-db-cleanup.log`；测试 schema 已清空，专用 PostgreSQL 已停止。

## 已知问题与验收边界

1. **Medium，继承 #109：** 取消 REVISION 后显式 retry 仍只 lookup occupied 位置，
   没有重新占用已回收的 idle 目录，可能 exit 65。
2. **Low：** INITIAL executor 未向控制面传递 adapter 已保留的启动诊断。
3. **Low，覆盖缺口：** 已知 merge 保留原位置供 ARCHIVE 缺少永久端到端回归。
4. 极深 Unicode 重复编码字符串仍可能触发 Python 递归上限；历史返回
   `history_unreadable`，最终答案仍可能失败。本次不新增通用深度策略。
5. 未完成真实 GitHub App installation-token 写入闭环和 Linux 原生 Amp 模型执行/取消验收；
   CI 镜像 smoke 不代表这些场景通过。原生历史可能滞后，不承诺离线可用，也不允许其他
   客户端并发继续 worker 正在执行的 thread。长期 worker 容器不按单次取消销毁。
6. managed policy、账户插件及服务端 MCP 仍需运维核查，CLI settings 不能充当权限隔离。

取消入口、权限和状态反馈优化由 [#108](https://github.com/b1indsight/agentflow/issues/108) 跟踪。
本次文档精简与 PR 状态调整不包含上述问题的修复或部署。

## 决策摘要

- 2026-10-21：原生导出和原生登录实测后，替换“只录 worker 输出”和“API key 必填”的
  初始假设；选择现有 CLI adapter，未采用 SDK、插件或额外结果工具。
- 2026-10-21：完成后端、历史与镜像适配；按实测收窄缺线程自动恢复及取消回收保证，
  修复 Unicode JSONL、凭据过滤、历史查询窗口和 adapter 诊断。
- 2026-10-22：跟随 #109 最终最小回收方案，确定 wire v15 / journal v4 / manifest v1；
  收窄脱敏职责和重复校验，明确模块/类名，删除容器配置文本断言。
- 2026-10-22：#109 合并后对齐 main；按用户要求将本文件收敛为当前方案、证据和未修项，
  完整调研、初始计划和逐轮变更保留在[精简前记录](https://github.com/b1indsight/agentflow/blob/b14da113d1d64551b00f0dbdcc96b5ff0f20c706/docs/plan/worker-amp-cli.md)。

## 后续变更记录

### [001] — 2026-11-06 — worker 继承现有 skills 与 MCP（待批准）

**阶段：** 方案待确认，尚未修改产品代码或测试。
**Bookmark：** `feat/worker-environment-inheritance`，目标 `main`。
**请求：** 用户要求“修改成继承现有环境的skill和mcp”，见
[本次讨论](https://ampcode.com/threads/T-01a1af9f-e151-71e0-b63c-4f11d7aed73b)。
本提案将替代前文“INITIAL 仅开放本轮 MCP、拒绝 workspace Amp 设置”的限制；原文保留为历史。

#### 目标、范围与当前问题

“现有环境”指启动 worker 的专用账号、HOME、原生 CLI 配置及任务仓库可访问的文件，
不复制 controler 或当前对话的配置，不把个人 HOME 挂载到 worker。
两个后端的 INITIAL、REVISION resume/fallback 都应保留该环境原有的 skills/MCP；
用户原本禁用的工具、MCP 和原生审批规则继续生效。

当前 Amp 每轮 `--settings-file` 替换用户配置，又禁用 skill、MCP 发现/调用工具并拒绝
workspace 设置；仅删除 skill 开关不足以恢复继承。Codex 已保留 HOME/CODEX_HOME，
但两个后端的环境白名单都会丢弃 `EXAMPLE_MCP_TOKEN` 一类自定义变量，造成配置可见、认证不可用。
依据为 [`amp_process.py`](../../app/worker/execution/amp_process.py)、
[`amp_agent.py`](../../app/worker/execution/amp_agent.py)、
[`codex.py`](../../app/worker/execution/codex.py)，以及 Amp 的
[配置文件规则](https://ampcode.com/docs/cli/settings)和
[MCP 配置、环境替换及优先级](https://ampcode.com/docs/customize/mcp)。

不改后端选择、模型、调度、UI、数据库、wire/journal 格式或交付权威；不新增依赖、继承开关、
skill manifest、配置解析器或兼容层。trusted-host、进程监督、Schema/VCS 核验和失败不重放不变。

#### 拟采用行为与实现位置

1. **Amp 使用原生配置。** `AmpCodingAgent._run()` 不再传替代用户配置的临时
   `--settings-file`；保留 `AMP_SETTINGS_FILE` 等原生配置入口。删除 `_workspace_settings()`
   的一律拒绝及 `_outcome()` 的固定工具集合上限，保留同 session 完整终态、退出码和原生 shell
   检查。不强制开启用户自己禁用的 skill、MCP 或工具，不自动批准 workspace MCP。
2. **只覆盖保留名称 `flow`。** 通过原生 `--mcp-config` 合并当前 assignment 的 `flow`，
   其他 server 保持原状；INITIAL 使用当前 endpoint/token，REVISION/fallback 显式禁用旧 broker。
   配置只含环境变量引用，token 不进入 argv 或配置文件。CLI 同名 MCP 优先级高于 workspace、
   user 和 skill；具体 JSON 形状、禁用和整项覆盖语义在实现前校准。
   Codex 保留现有 `_mcp_args()` 的同名覆盖方式。
3. **环境改为继承后过滤受控项。** 在现有
   [`credentials.py`](../../app/worker/execution/credentials.py) 内增加一个共享函数
   `inherited_agent_environment(source: Mapping[str, str]) -> dict[str, str]`，供
   `amp_environment()` 和 `_codex_environment()` 使用，避免两套规则漂移：
   - 保留 HOME、PATH、原生配置位置、CLI 登录变量，以及 MCP/skill 所需的自定义变量；
     Amp 的更新检查仍由 `AMP_SKIP_UPDATE_CHECK=1` 关闭。
   - 先移除全部 ambient `FLOWTOOL_*`、`GH_*`、`GITHUB_*`，以及 Git 凭据/配置/仓库重定向、
     askpass、SSH 认证入口；保留既有 Git 作者/提交者 NAME、EMAIL 语义。
   - 移除父 agent 的运行时 thread 身份变量，例如 `AMP_CURRENT_THREAD_ID`，不继承父会话身份。
     不按整个 vendor 前缀一律删除，否则会误删原生配置入口或合法 MCP 变量。
   - 最后沿用 context 注入本轮 push/MCP token、Git helper 和独立 `gh` 配置；无 context 时
     不带回上轮能力。上述保留名称不能用作用户自定义 MCP 凭据变量。
4. **去掉旧策略残留。** 删除编码路径的临时全量 settings 和 `AMP_TOOLS` 固定集合，
   同步处理 `AmpHistory._refresh()` 对旧 settings helper 的引用；历史仍只调用原生 export，
   不执行模型请求，不改变并发、缓存、预算与公开块投影。更新 README、部署说明和架构说明。
5. **凭据和日志边界。** 保持已有受控凭据脱敏，不记录整份环境、用户配置或 MCP headers；
   不为任意变量名新增启发式密钥识别框架。现有脱敏不保证识别自定义变量、配置文件或第三方
   MCP 输出里的所有秘密，这一限制必须写入部署说明，不能把继承功能宣传为隔离或全面防泄漏。

不增加新的数据类型、持久化字段或公共 API；除共享环境函数外，优先修改/删除现有函数。
环境过滤只用于 agent 子进程，不放宽独立 VCS 核验进程的最小环境。

#### 取舍、风险与错误处理

- **选择原生配置 + 单项 MCP 覆盖。** 自行读取 JSON/JSONC、复制 skills 或模拟配置优先级会
  重复 CLI 的职责；继续使用临时全量 settings 会漏掉用户配置。只删除禁用项仍不能保留自定义
  MCP 环境变量；完全不做环境过滤又会带入 control/worker 凭据和旧 assignment 能力。
- **继承范围明确扩大。** Amp 原生工具/插件策略也随配置生效，可能比旧白名单开放更多工具；
  自定义 MCP 和 skill 能看到继承的变量。过滤表无法识别任意名字的控制密钥，不能保护磁盘上
  的密钥或阻止 full-access agent 另行启动程序，仍须专用账号或容器/VM。
- **GitHub 例外保留。** `GH_TOKEN`/`GITHUB_*` 不原样继承；依赖这些变量的 MCP 必须使用
  专用凭据配置。Git/gh 的受控执行继续使用 assignment capability，不回退个人登录。
- **原生限制不绕过。** 用户或 managed policy 禁止 `flow`/shell、workspace MCP 未批准、
  原生配置损坏、MCP 凭据失效时，保留 CLI 的失败或待审批行为；不改全局配置、不自动批准、
  不换账号、不扩大权限、不静默重跑。对旧 Amp thread 是否保留创建时工具限制进行单独校准，
  不承诺仅更新 worker 就会改写既有 thread 的工具集。

#### 实施顺序与验收

1. 用户确认本方案及上述继承/凭据边界后，先用只读 CLI 和临时环境校准配置合并；
   本机 CLI 与 Dockerfile 固定版本分开记录，不能用新版本结果代替发布版本。
2. 更新现有 Amp start/resume 测试与 Codex 环境测试，证明自定义 MCP 变量可达、原生配置
   不被替换、workspace 配置不被拒绝、额外工具不导致结果失败；同场景旧实现应失败。
3. 实现共享过滤、Amp 原生配置和当前 `flow` 覆盖，清理 history helper 耦合及过时文档。
4. 验证首次启用及修订禁用 `flow`、保留其他 MCP、保留用户禁用/审批策略；使用隔离的
   fixture HOME、user/workspace JSONC、可发现的测试 skill 和无外部副作用的本地 MCP。
   用真实 CLI 的 skill/MCP 列表及必要的协议发现检查实际加载与变量替换，不调用真实外部工具，
   不通过 `amp -x` 或 `threads continue` 启动 agent 作为测试捷径。
5. 运行相关 driver/history/worker 执行回归、`uv run ruff check app tests`、
   `uv run mypy app tests`，并按 CI 构建步骤检查打包。没有 UI 改动，不增加浏览器验收。
   推送产品代码前执行仓库 review gate。

消融只针对必要边界：恢复临时 settings 应使继承用例失败；去掉环境过滤应被受控凭据回归捕获；
去掉 revision 的 `flow` 禁用应暴露旧 broker。验证通过后只保留公共执行合同的代表性测试，
不为简单字典过滤逐项增加永久测试。无数据迁移；更新并重启空闲 worker 后生效，存量 Flow
不跨后端迁移。回退此变更恢复原启动策略，不删除或重写原生配置。

#### 当前证据、待确认与未实施项

- 已 fetch 并核对最新 `origin/main`；与本需求有关的 Amp/Codex 源码和原 feature record
  与当前检出一致。原工作区的 console 改动未动，方案放在独立 jj workspace。
- 已核对本机 Amp CLI help 和官方文档；没有执行 worker 编码任务、修改个人配置或调用 MCP 业务工具。
  真实合并、disabled 行为、skill 加载和认证环境验收均在实施阶段完成，目前不算已验证。
- 初稿咨询实际使用 `claude-opus-5-5`，初始化及响应模型一致，tools/MCP 均为空，进程成功；
  主代理核对并删去 vendor 前缀白名单、启发式 secret helper 和新增事后 flow 工具检查等额外机制。
- **批准前需确认：** 是否接受原生 skills/MCP/工具策略及自定义环境变量继承，同时保留上述
  flow、control/worker、Git/GitHub 凭据例外；现有信息不需要新增产品选项。
- **实现中待校准：** 固定 CLI 的 MCP overlay 形状与替换规则、旧 thread 工具集、原生审批
  表现，以及历史 export 去掉临时 settings 后仍只读的行为。证据不满足时报告限制，不造兼容层。
- **交付状态：** 本条仅为方案；尚未改实现或测试，尚未部署。配置分发、跨机器 skills 同步、
  通用 secret 检测和第三方 MCP 的授权管理不在本次范围内。

### [002] — 2026-11-06 — 原生配置继承实现与 CLI 校准

**触发与范围：** 实施 [001]，替代原有全量 settings、workspace 拒绝及环境白名单；
保留 trusted-host、Schema/VCS、逐轮 Git/gh 能力、进程监督与已有脱敏。修改仅在独立
`worker-environment-inheritance` workspace，原检出的 console 工作未动。

**实际实现：** `credentials.inherited_agent_environment` 统一两个后端的受控变量过滤；
`amp_agent` 仅用 `--mcp-config` 覆盖 `flow`，接受原生 skill/MCP 等额外工具；
`amp_process` 删除全量 settings helper，`AmpHistory` 直接使用原生只读 export。
Codex 的 flow 覆盖和禁用参数不变；两个后端都保留自定义 MCP 变量。同步更新 README、
架构、部署说明及 changelog；无配置开关、JSONC 解析器、依赖或协议变更。

**决定与取舍（已实现）：** 固定 Amp `0.0.1792555249-g702cd1` 与本机
`0.0.1793937652-gdc5d48` 都接受平铺的 `{"flow": {...}}` overlay，并整项替换同名
server。`mcp list --json` 会忽略未知字段，不能证明运行期 schema 有效；实际 `mcp doctor`
在设置 `disabled: true` 后仍连接 fixture server（`disabled-schema.log`）。因此用
`http://127.0.0.1:0/mcp` 和无旧 token 实现修订轮旧 broker
不可用，取代 [001] 尚待校准的“显式禁用”形式。选择端口零避免复用旧 endpoint，且不引入
假 MCP 子进程或配置合并器；代价是 CLI 会重试并显示连接错误，不宣称原生 disabled 状态。

**验证与消融：** 证据汇总见[本次验证记录](https://ampcode.com/artifacts/worker-skills-mcp-57f5fb2b-f3cd-413e-9434-e3dc3ee86d83)。
本地原始日志保存在 `.amp/in/artifacts/worker-environment-inheritance/`，不随代码发布。

| 检查 | 实际结果与复现条件 |
| --- | --- |
| 回归先失败后通过 | `pytest tests/test_amp_driver.py tests/test_codex_driver.py -q`：旧实现拒绝 workspace 设置并丢弃自定义变量；新实现通过。随后加入已有 history 回归一同通过。日志 `before-tests.log`、`targeted-tests.log` |
| 原生 CLI 校准 | 两个版本分别使用 fixture HOME/XDG、`AMP_SETTINGS_FILE` JSONC、任务仓库 JSONC、全局/Claude/repo skills、本地 stdio MCP 与真实 WorkerMCP。执行 `skills list`、`mcp list --json`、`mcp doctor`、`tools list`；skill 可发现、ambient MCP 收到测试变量、当前 flow 的 Bearer 环境替换认证成功。仅协议发现，未调用业务工具 |
| 原生权限 | `mcp doctor` 保留 user `amp.mcpPermissions` 的 reject；workspace server 仍 awaiting approval，未启动其子进程；未写个人配置或批准任何 server |
| 去掉 flow overlay | 同一 fixture 下带 overlay 不启动旧 flow；去掉 overlay 后旧 flow 的 stdio 服务被启动。两版本结果一致，保留单项覆盖。日志 `cli-calibration.log` |
| 恢复旧 settings | 用父提交的真实 `write_amp_settings` helper 与同一 fixture 执行 `mcp list --json`：恢复 `--settings-file` 后 ambient server 消失；原生配置保留它，故删除替换策略 |
| 去掉环境过滤 | 在临时进程中将两处共享 helper 替换为 `dict`，运行既有环境回归；两个后端都暴露受控变量并失败，故保留共享过滤。无产品文件改动，日志 `ablation.log` |
| 本地验收 | 临时 PostgreSQL 17 上全量 `pytest -q --tb=short` 通过，无跳过；`ruff check app tests`、`mypy app tests` 通过。`python scripts/build_console.py`、`uv build --no-sources` 及 wheel 内静态入口检查通过 |

**证据边界：** 全量测试使用本机 Python 3.13，打包使用本机 Node 25，CI 分别使用 Python
3.12、Node 24；本机无 Docker，未验证 Linux 运行。本次没有启动真实模型或调用真实 GitHub
业务工具，没有验证旧 Amp thread 的创建期工具集是否刷新。`tools list` 在设置
`amp.tools.disable` 后仍显示该工具，不能作为运行期禁用生效的证据；worker 不覆写这些设置，
仍由原生 CLI 执行。虚拟 Amp key 对账户 MCP/export 返回预期 401；export 失败路径没有启动
fixture MCP，但成功导出及账户端 MCP 未在本次重新实测。历史投影/分页/脱敏由既有回归覆盖。
上述限制已写入部署说明；不增加兼容或权限绕过机制。

**交付边界：** 实现和本地检查完成，review/CI 状态以同一 [PR #128](https://github.com/b1indsight/agentflow/pull/128)
为准；未合并、未部署、未修改运行中的 worker。更新并重启空闲 worker 后使用新行为。

### [003] — 2026-11-06 — 编码空闲超时与失败诊断（待批准）

**阶段：** 方案待确认，尚未修改产品代码或测试。
**Bookmark：** `fix/worker-idle-timeout`，目标 `main`。
**PR：** [#131](https://github.com/b1indsight/agentflow/pull/131)；本次计划与后续实现使用同一个 PR。
**需求：** 用户要求修复错误提示，并将 20 分钟上限改为“最后一次活动之后，连续
20 分钟没有任何新活动才触发”，见[本次讨论](https://ampcode.com/threads/T-01a1af9f-e151-71e0-b63c-4f11d7aed73b)。
本条提案替代原方案的编码总时限约定，并处理“已知问题”第 2 项；其他隔离、继承和交付边界不变。

#### 动机、目标与范围

Flow 3 / job 14 达到默认 1200 秒时被终止，退出码为 124；当时已有本地实现提交，
正在等待审查，尚未完成 push 和 `flow.open_pr`。
[运行证据与排查](https://ampcode.com/artifacts/worker-skills-mcp-57f5fb2b-f3cd-413e-9434-e3dc3ee86d83)。
当前 Amp、Codex 各有绝对运行时限，REVISION 还套有相同长度的外层 deadline。
INITIAL 丢弃 adapter 的 `failure_detail`，controler 又把后端名称写死为 `codex`、
将所有失败阶段标作 VCS，导致实际失败原因不清楚。

本次覆盖两个 CodingAgent 后端的 INITIAL、REVISION resume/fallback：持续有新活动即可
继续执行，连续静默达到配置值才超时。默认仍为 1200 秒，不通过提高默认值解决问题。
不改模型、任务调度、skills/MCP 继承、wire/journal/数据库格式或 GitHub 操作权威；
不新增依赖、自动重试、活动持久化、CPU/文件变化探测或通用进程管理框架。

#### 可观察活动与用户行为

- **活动来源：** 直接 CLI 的 stdout 或 stderr 读到非空新数据；包括 CLI 传出的模型消息、
  工具调用、工具结果和进度输出。半行或分块数据同样计入，不等待完整 JSON/换行。
- **计时起点：** CLI 成功启动时使用单调时钟开始计时，每次读到新数据刷新期限。
  进程首次始终无输出，也在一个空闲窗口后终止。
- **不续期：** stdin 写入、EOF、worker WebSocket 心跳、历史查询、轮询、进程仅仅存活，
  以及未被 CLI 转发的 broker/文件活动。不会注入合成心跳使卡住的代理永久存活。
- **例子：** 第 19 分钟有新工具结果，再等连续 20 分钟无输出才超时；持续有输出的任务
  即使运行 50 分钟也不会因本次编码时限被终止。先持续活动、后静默时，从最后一次输出计时。
- **明确限制：** stdout/stderr 是可观察活跃度，不等于任务取得有效进展；持续诊断/重试日志
  也会续期。反之，子工具若 20 分钟没有任何输出被 CLI 转发，仍会触发空闲超时。
  本次不解析各 vendor 的私有日志或推断静默工具是否仍在做有效工作。
- **失败结果：** 空闲超时仍返回 124，并明确说明后端、连续无新输出及配置时长。
  初始失败显示准确的 `execution` 阶段和安全详情，不再误标 Codex 或 VCS；
  没有 adapter 详情时保留中性失败提示，不将异常退出误判为成功。

#### 最小实现与接口

1. **共享空闲等待，不共享整套进程监督。** 在
   `app/worker/execution/idle.py` 增加一个供两个 adapter 使用的内部函数：

   ```python
   async def wait_for_inactivity[T](
       io: asyncio.Future[T], *, timeout: float, last_activity: Callable[[], float],
   ) -> T:
       ...
   ```

   `io` 是调用方拥有的 stdout/stderr/进程等待组合 future；`timeout` 是正有限空闲秒数；
   `last_activity` 返回与事件循环时钟同源的最近读到数据时间。函数使用剩余空闲时间等待，
   每次唤醒重新计算；io 完成时返回结果或传播原异常，只有 io 未完成且窗口耗尽才抛
   `TimeoutError`。它不拥有进程或取消 io，清理由原调用方负责。无需新类或持久化字段。

2. **Amp 编码与历史导出显式区分。** `run_amp` 保持已有参数和结果类型，新增一个关键字参数：

   ```python
   async def run_amp(
       args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
       input_text: str | None = None, timeout: float = 1200,
       max_stdout: int = 8 * 1024 * 1024,
       timeout_kind: Literal["total", "idle"] = "total",
   ) -> AmpProcessResult:
       ...
   ```

   `AmpCodingAgent._run()` 显式选择 `idle`；两个 reader 更新同一个最近活动时间。
   `AmpHistory._refresh()` 继续选择总时限，保持现有 20 秒 export 上限及查询预算，
   持续输出也不能使历史请求无限延长。保留 Amp 字节上限、完整终态检查、脱敏与回收链；
   超时原因含 `self.timeout`，不靠解析错误文案决定业务分支。

3. **Codex 按流读取。** `CodexAgent._run()` 用两个 reader 和进程等待替代一次性
   `communicate()`，复用上述空闲等待函数，保留当前 stdout/stderr 解析、最终答案文件、
   session 校验和已证明的执行前拒绝 fallback。超时返回明确的空闲诊断；所有退出路径
   都回收进程并取消/等待 reader，不能因后代持有管道而无限等待。保留现有进程组清理策略，
   不借此重构两个后端的退出机制或给 Codex 增加另一套输出政策。

4. **移除会提前截断活跃编码的外层总时限。** 从 `WorkerExecutor.__init__` 删除
   `revision_timeout` 参数和属性，`execute()` 的 REVISION 分支直接等待 `_revision()`；
   `WorkerRuntime.__init__` 不再传入该参数。每次编码调用由 adapter 管理空闲期限，
   既有 VCS 单命令总时限（当前 30 秒）及取消/回收边界保留。
   不承诺整个 Flow 的总耗时有界，也不把其他阶段的独立请求超时改成空闲时限。

5. **完整传递失败原因。** `WorkerExecutor._initial()` 的非零退出分支设置
   `failure_stage="execution"`，复制已脱敏的 `run.failure_detail`，缺失时使用与 REVISION
   一致的中性执行失败说明。不新增协议字段或把超时当作可自动重放的执行前拒绝。
   `app/controler/workflow/results.py` 的初始失败文案移除硬编码 `codex` 和一律 `VCS`
   前缀，显示实际 stage 与 detail；没有 stage 但有 detail 时也不丢弃已有原因。
   示例：`编码失败（exit=124，阶段=execution）：Amp 连续 1200 秒无新输出，执行超时。`
   `result`、`delivery`、`push` 等既有阶段继续准确显示。

6. **配置与文档同步。** 保留 `FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS` 与
   `FLOWTOOL_CODEX_RUN_TIMEOUT_SECONDS` 名称及 1200 默认值，将它们明确为编码空闲秒数；
   两者均要求正且有限。更新 README、部署说明、架构中的单轮/修订总时限描述和 changelog，
   保留历史查询、VCS 命令、恢复/租约的独立时限说明。现有配置在新 worker 上按新语义解释，
   不保留旧编码绝对时限作为 fallback。

#### 取舍、风险与安全边界

- 单纯改大总时限仍会终止更长的活跃任务；只改 Amp reader 会漏掉 Codex 和 REVISION
  外层 deadline；把 `run_amp` 全部改为空闲计时会破坏历史查询的有限请求预算。
- 一个短小等待函数复用两个后端的单调时钟、完成优先与静默判定；不合并两套已有进程监督。
  `timeout_kind` 的两个值对应真实的编码和只读导出调用者，不增加更多策略选项。
- 已完成 io 或已观察到的输出超限应优先于空闲判定；进入超时清理后不因晚到数据改回成功。
  活动时间以 reader 实际读取为准，不承诺精确获知内核管道中尚未调度读取的数据时间。
- 取消必须穿透等待函数，由已有 finally 路径先回收进程/reader 再返回；EOF 后进程不退出、
  进程退出而后代仍持有管道等场景也不能永久占槽。仅“进程仍活着”不触发续期。
- 失败详情沿用 adapter 和终态写入的现有脱敏，不输出原始 stream、环境、MCP headers 或
  token；自定义秘密的识别限制不因本次修复消失。
- 持续活跃执行可能长于现有 GitHub capability 有效期；到期后的 push/交付仍可能失败。
  本次不隐式刷新凭据、不换认证来源、不另设编码总时长限制。

#### 实施顺序、验证与消融

批准后先在现有 driver/executor/controler 测试入口复现旧行为，再实现空闲等待、
两个 adapter 接线、外层 deadline 移除与诊断透传，最后同步文档和发布说明。
永久测试只覆盖调用方依赖的行为；时间判定优先使用可控时钟与受控流，避免真实等待 20 分钟。
真实 Python 假 CLI 仅用于确认管道/进程组集成，不调用真实模型或 GitHub 业务工具。

| 场景 | 验收标准 |
| --- | --- |
| 持续 stdout 或仅 stderr 活动，累计跨越多个空闲窗口 | Amp/Codex 正常完成，不因总时长失败 |
| 首次无输出；有活动后停止；半行/分块输出；EOF 后仍不退出 | 仅非空新读入续期，达到完整静默窗口才返回 124 |
| 取消、进程退出与截止同时发生、输出上限 | 保持取消传播、已完成结果及异常权威，完成既有回收，不把超限改判为超时 |
| REVISION resume 与 fallback 的公共执行路径 | 持续活动可跨越旧总时限；静默失败不触发 fallback，既有执行前拒绝规则不变 |
| AmpHistory 持续输出但超过 export 预算 | 仍按总时限返回 query_timeout，不因编码改动续期 |
| INITIAL → 终态 → controler 失败结果 | 保留 execution 与实际安全诊断，文案不误写 Codex/VCS；其他阶段与无详情情况仍清楚 |
| 安全与默认值 | 两后端默认仍为 1200；已知能力 token 不随错误信息泄露 |

消融对照：临时移除续期、恢复 REVISION 外层 deadline、误将 history 切为 idle、去掉
INITIAL 详情透传，分别必须被对应行为回归捕获；临时对照改动不保留。
运行相关 driver/history/executor/runtime/controler 回归，随后 PostgreSQL 全量测试、
`uv run ruff check app tests`、`uv run mypy app tests` 和现有打包检查。
没有 UI 布局改动，错误文本通过实际 controler 结果断言验证；不新增无关浏览器截图测试。
代码推送前执行仓库 review gate，继续使用本次草稿 PR，不另开实现 PR。

#### 发布、待确认与当前状态

- 无数据迁移；worker 更新并在空闲时重启后使用新语义。controler 也需更新才能修复显示；
  现有字段兼容，先更新 controler 或同步更新两端，避免旧 controler 把新 execution 详情误标为 VCS。
- 不热补丁正在运行的 worker，不自动重试 Flow 3，不清空 journal/工作区或处理已有未推送提交。
  失败 Flow 是否重试在实现和发布验证后另行确认。
- 需要批准的是上述活动边界与实现范围。真实 CLI 在长工具运行期间转发进度的频率需在
  实施时校准；即使不转发，也按已明确的静默语义处理，不隐式改为“子进程存活即活跃”。
- 初稿咨询实际使用 `claude-opus-5-5`，初始化和响应模型一致，tools/MCP 为空且成功返回；
  主代理核对源码后收窄为一个泛型等待函数、显式超时模式，并确认 VCS 单命令默认值为 30 秒。
  方案位于独立 jj workspace，开发检出的其他改动保持不动。本条只记录提案，不表示已实现。

### [004] — 2026-11-06 — 编码空闲超时与安全失败详情实现

**需求与状态：** 按 [003] 实现持续输出续期和准确失败提示；继续使用
`fix/worker-idle-timeout` / [#131](https://github.com/b1indsight/agentflow/pull/131)。
在独立 jj workspace 中基于最新 main 实施，未修改开发检出的其他工作、运行中的 worker、
Flow 3 journal 或其未推送成果；尚未部署，不自动重试。

#### 实际改动与取舍

- `execution/idle.py` 只负责等待调用方拥有的 I/O future，使用事件循环单调时钟重新计算
  剩余空闲窗口，完成结果或异常优先于静默判定；取消和进程/reader 回收归 adapter。
  没有新增时钟任务、活动持久化或通用进程框架。
- Amp 两路 reader 和 Codex 新增的分块 reader 在读到非空字节后续期，不等换行。
  `AmpCodingAgent` 显式选择 idle；`run_amp` 默认仍为 total，因此历史 export 保持原预算。
  Codex 去掉整轮 `communicate` 总时限，以受保护的 cleanup 回收进程组并 join reader；
  cleanup 的 `proc.wait` 有独立 5 秒上限，避免后代持有管道时无限等待。
- 删除 executor 的 `revision_timeout` 参数、属性和外层 deadline，runtime 只向 adapter
  传入对应空闲秒数。VCS 命令和历史查询的独立总时限不变；两后端配置都验证为正有限值。
- 两后端超时仍返回 124，安全诊断包含后端、`no new stdout/stderr` 与配置秒数。
  Amp 超时不再追加任意长度的启动 stderr，避免尾部裁剪把核心超时原因挤掉。
  INITIAL 设置 `execution` 并透传安全详情，缺失详情时沿用 REVISION 的中性提示；
  controler 的通知与持久错误保留实际 stage/detail，无 stage 的详情也不会丢失。
- README、部署与 review-loop 文档同步新语义和发布要求。没有修改协议、数据库、
  凭据来源、Schema/VCS 交付权威或保守 fallback；既有成功、失败、脱敏与取消回归保留。

#### 验证与机制消融

先在旧实现上运行新回归，复现持续输出仍被总时限终止、超时详情不完整和 INITIAL 丢失
execution/detail。修改后，受控单调时钟和真实 `StreamReader` 验证两后端 start/resume：
持续活动跨越多个 1200 秒窗口、仅 stderr、半行、首次静默、最后活动后的完整静默窗口、
晚到 EOF、进程已退出但管道未关闭、完成/截止竞争、取消及 reader 回收。
公共 REVISION 路径带真实本地 Git 核验，resume 与 fallback 均可跨越旧外层 deadline。
Python 假 CLI 子进程另行确认真实管道续期、Amp 输出上限与进程组清理；不调用模型。

四项进程内消融分别执行同一组输入，不改写产品源码；下列简化均被对应行为回归捕获：

| 对照 | 观察 | 决定 |
| --- | --- | --- |
| 用固定总等待替代活动续期 | 持续 stdout/stderr 的两后端 start/resume 提前结束 | 保留共享空闲等待和双 reader 续期 |
| 恢复 1200 秒 REVISION 外层 deadline | 活跃修订的 resume/fallback 被取消 | 保持外层总时限删除 |
| 历史 export 也选择 idle | 持续输出的导出越过请求预算，未返回 query_timeout | 保留显式 total/idle 区分 |
| INITIAL 丢弃 stage/detail | worker 公共执行结果丢失安全诊断 | 保留详情透传和中性 fallback |

针对性测试、Ruff、Mypy、控制台 TypeScript/Vite 构建、wheel/sdist 打包与 wheel 中静态入口、
空闲 helper 检查通过。构建保留既有大 chunk 提示。首次全量运行遇到旧凭据回收测试仍只模拟
`communicate`、等待启动信号不返回；已将该测试改为 reader 模拟，并单独验证凭据临时目录
保留到进程回收结束。消融脚本是一次性本地验证，不进入仓库。
最终通过 `FLOWTOOL_TEST_DATABASE_URL` 指向独立本机 PostgreSQL 实例运行全量 pytest，
全部通过、无跳过，仅有既有 Starlette/httpx 弃用警告。全量日志保存在 `full-pytest.log`。

可复现入口：`python -m pytest -q tests/test_worker_idle.py tests/test_amp_driver.py
tests/test_codex_driver.py tests/test_worker_executor.py tests/test_worker_runtime.py
tests/test_worker_github_environment.py tests/test_coordinator.py tests/test_revision_delivery.py
tests/test_conversation_history.py tests/test_config.py`；静态检查使用 `python -m ruff check app tests`
及 `python -m mypy app tests`，打包使用 `python scripts/build_console.py`、`uv build`。
本地使用已有 Python 3.13 开发环境，但从独立 workspace 导入代码；未改共享虚拟环境。
证据存于 `.amp/in/artifacts/worker-idle-timeout/`，汇总见
[本线程验证记录](https://ampcode.com/artifacts/worker-skills-mcp-57f5fb2b-f3cd-413e-9434-e3dc3ee86d83)。

#### 活动来源与验收边界

复核既有真实 Amp CLI 捕获：原生 shell 与 MCP 工具调用出现在 stdout 的 assistant 事件中，
shell 结果出现在 user 事件中；本机 `codex-cli 0.162.1` 的 exec 与 resume 帮助均声明
`--json` 将事件输出为 stdout JSONL。此次没有重新执行真实模型或 GitHub 业务工具，
也没有宣称长工具一定实时转发中间进度。
静默工具仍可能超时，持续诊断也能续期；GitHub capability 可能先于活跃任务到期且不续签。
这些限制与 [003] 一致，不使用进程存活或合成心跳规避空闲超时。
