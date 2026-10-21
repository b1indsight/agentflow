# Worker 仓库自动初始化与 bot GitHub 交付 — Feature record

**Stage:** Implementation — 本地实现、隔离回归与消融已通过；独立审查与远端 CI 另行确认，未合并、未部署。
**PR:** [#103](https://github.com/b1indsight/agentflow/pull/103)；沿用原 bookmark/PR，不混入工作区其他功能。
**Bookmark:** `fix/worker-repo-bootstrap`

阅读顺序：初始计划定义自动 clone，[001] 增加 bot gh 包装，[002] 详解 Git push 认证、
MCP 请求与 controler 创建 PR 的完整链路，[003] 修订为副本复用与 Git worktree 溢出，
[004] 确定按 worker 独立、单仓库位置上限默认 2 且可配置。
实施采用初始计划和 [001]–[004] 的适用决定，当前实施记录见 [005]。
早期条目的“仅本地、未提交”等是当时状态；当前方案以最新适用条目为准，发布计划不表示批准实施。

## 1. Initial plan

### 背景与目标

首次派发 INITIAL 时，[WorkerExecutor](../../app/worker/execution/executor.py) 将目标映射到
`<worker_root>/repos/<owner>/<repo>`，再调用
[`prepare_flow_repo`](../../app/worker/execution/vcs.py) 复制 seed。seed 不存在时直接抛出
`FileNotFoundError`，编码 agent 尚未启动，没有机会自行 clone。
[原 VCS 计划](m1-code-agent-vcs.md#22-本次不包含) 明确要求预置目标仓库；本计划扩展该范围。

目标是新 worker 无需人工预置每个仓库：收到已授权的任务后，自动拉取目标仓库并准备原有的
Flow 独占副本。保留 control 下发短期能力、worker 不持有 GitHub App 凭据的边界。

### 范围与验收标准

1. INITIAL 对应 seed 不存在或是空目录时，从 `https://github.com/<owner>/<repo>.git`
   clone，检出任务的 `base_branch`，配置任务的 `remote`，随后创建完整 Flow 副本。
2. 已有可用 seed 保留原有 Git/jj 元数据和工作区，不自动 fetch、checkout、reset 或覆盖。
   已有 Flow 副本直接复用，不因 seed 缺失重新下载，也不改动该 Flow 的已有进度。
3. 私有仓库使用当前 assignment 的短期 push capability 完成读取；token 不写入 URL、argv、
   仓库配置或日志，不继承 worker/control/MCP 密钥。
4. clone 失败、超时或取消不留下可被误认为成功的 seed；返回脱敏的准备阶段错误，不启动 agent。
5. REVISION 继续要求原有工作区与冻结交付匹配，ARCHIVE 继续遵守受管路径清理约束；两者都不自动 clone。

这里的“初始化”指 clone 建立 `.git`、remote 与基线 checkout。不强制执行 `jj git init`，
不代替 agent 选择 Git/jj，不自动安装项目依赖、运行项目初始化脚本、创建提交或推送。
新 clone 默认具备 Git 元数据；预置 jj 仓库及 agent 的既有 VCS 自主选择不变。
无提交的远端仓库或不存在的基线分支报告准备失败，不自动创建初始提交。

### 拟议行为

```text
收到 INITIAL + 有效的当前仓库 capability
  → 校验仓库身份与受管路径
  → 同 Flow 副本已存在：直接复用
  → 否则确认 seed；缺失/空目录时先 clone 到临时目录
  → clone 成功并确认基线可用后，原子发布 seed
  → 沿用完整复制与 VCS 快照，再启动编码 agent
```

非空但没有自身 Git/jj 元数据的目录、文件以及 symlink 路径拒绝处理，保留现有内容。
目录位于另一个仓库内时，不能把祖先仓库的元数据误当成目标仓库；检查必须绑定 seed 本身。
仓库标识必须是合法的 `owner/repo`，拒绝路径逃逸和与现有 Flow 目录命名冲突的保留标记。
不增加任意 clone URL、额外凭据、后台扫描或新的配置开关。

### 技术设计与受影响模块

- `app/worker/execution/vcs.py`：负责 seed 首次准备及受管路径校验。复用现有有界子进程执行、
  取消后的进程组回收，以及隔离的 Git 环境。临时 clone 与目标位于同一文件系统；成功校验后
  原子 rename，失败只清理本轮明确拥有的临时目录，不递归删除用户原有目录。
  保留 `prepare_flow_repo` 的完整复制与同 Flow 复用语义，并先检查已有 Flow 副本。
- `app/worker/execution/executor.py`：在 capability 校验后接入 seed 准备，传入任务的仓库、
  基线与 remote。准备失败转成明确的 `InitialTaskResult`，不透传可能包含凭据的 Git stderr。
  VCS 快照使用与 clone 相同的任务 remote；不改动修订和归档编排。
- `app/worker/execution/credentials.py`：复用现有 `git_credential_environment()`，
  不添加第二套凭据存储或认证方式；仅在确有必要时调整实现。
- `tests/test_vcs.py`、`tests/test_worker_executor.py`：覆盖从空 worker root 开始的准备，
  以及失败不启动 agent 的合同；按接口变化更新现有替身，不为每个 helper 增加测试。
- `README.md`、`docs/deployment/README.md`：说明自动准备触发时机、目录位置、所需读取权限、
  Git/jj 行为及失败处理，保留现有工作区中的控制台和部署相关改动。

继续使用 `AssignmentSecret.push`；具备仓库 contents 写权限的当前能力也用于 clone 读取。
clone 进程使用无个人 Git 全局配置、禁止交互认证及 hooks 的环境，凭据仅通过已有临时
credential helper 注入。实现时确认命令级 helper 设置不会屏蔽当前 capability。
不在本项目工作目录运行 clone，避免继承其仓库配置。

### 备选方案与取舍

- **继续人工预置：** 无需实现，但新 worker 仍会在第一个任务失败，无法满足自动准备要求。
- **让编码 agent 自行 clone：** agent 启动前就需要有效的 cwd 和 VCS 基线，不能解决当前失败点。
- **直接 clone 每个 Flow，删除 seed：** 省去首次 seed 准备，但改动现有预置 jj、复制、路径和归档
  合同更大；本次保留 seed 与 Flow 完整副本的架构。
- **所有仓库强制初始化 jj：** 改变现有 VCS 自治合同并引入额外前置；本次只提供 Git clone 基础。

### 成本、风险与限制

- 首次任务增加网络与磁盘成本。采用现有有界 VCS 命令执行；网络、权限或大仓库导致超时时，
  明确失败并可由用户处理后重试，不静默重跑 agent。
- clone 中断可能留下临时数据。正常错误、超时及取消路径负责清理；进程被强制终止时的残留
  不作为 seed 使用，后续任务另建临时目录，不新增通用磁盘垃圾回收服务。
- 复用已有 seed 保持当前行为，也意味着本次不解决后续新 Flow 的基线自动刷新；不为此 reset
  用户预置仓库。该限制需在部署说明中明确。
- 测试只访问本地 fixture remote，不访问真实私有仓库、不运行真实编码 agent，也不修改远端共享状态。

### 实施顺序

1. 实现路径/元数据校验、缺失或空 seed 的原子 clone，以及已有 Flow 优先复用。
2. 接入 INITIAL 的当前 capability、基线和 remote，统一脱敏的准备失败结果。
3. 添加代表性回归，更新部署说明，执行验证并在本记录追加实施证据。

### 验证与消融策略

- **已执行的现状验证：** 原代码下，缺失 seed 的真实调用抛出 `FileNotFoundError`；
  在本仓库内部放置空 seed 时还观察到 VCS 向祖先仓库发现元数据，最终因祖先 SSH remote 被拒绝。
  此现象说明不能只检查目录存在或依赖向上发现；尚未对所有空目录部署位置作普遍结论。
  现有 `python -m pytest -q -p no:cacheprovider tests/test_vcs.py tests/test_worker_executor.py`
  结果为 **13 passed**，没有覆盖首次自动 clone。
- **拟新增合同验证：** 使用真实本地 bare remote，在命令边界核对 GitHub URL 与临时凭据，
  执行真实 clone/checkout。覆盖缺失及空 seed、非默认基线/remote、已有 seed/Flow 的保留与隔离；
  clone 失败及取消不发布 seed，失败结果不含 token，agent 不启动。
  输入边界验证覆盖非空目录与 symlink/路径逃逸，不访问真实 GitHub。
- **回归：** 运行 VCS、executor、revision、archive、worker client/session 相关 pytest，
  再运行 `uv run ruff check app tests`、`uv run mypy app tests` 和 `git diff --check`。
  根据实际改动范围扩大测试；未执行的检查明确报告。
- **消融：** 实现后临时移除首次 seed 准备，确认缺失仓库的代表回归重新失败；恢复后重跑相关测试。
  不为简单参数传递设计额外消融或保留临时验证代码。

### 开放问题与后续工作

待用户批准上述范围；尤其确认“初始化”采用 Git clone 的含义，不含强制 jj 或项目依赖安装。
自动刷新已有 seed、submodule/LFS 专门支持、跨 worker 重建与自动清理强制退出残留均不在本次范围内。
当前未提交、未推送、未开 PR，产品实现与测试未修改。

## 2. PR change timeline

### [001] — 2026-10-20 — 初始化时准备 bot 身份的 gh 包装

#### 需求与状态

用户新增要求：“当你初始化的时候，使用 installation token 初始化一个 gh 的包装，
使得 worker 所有的 gh 交互通过 bot 来实现”。
本条扩展初始计划，覆盖 INITIAL、REVISION resume 和 fallback 中受管的 gh 调用；
不只在首次 clone 时配置一次。以下机制和权限边界为拟议方案，待用户批准，尚未实现。
已有 seed/Flow 的保留、原子 clone、失败清理及 ARCHIVE 约束不变。

#### 拟议行为与权限边界

1. controler 继续为每个 assignment 签发单仓库 installation token；worker 不接收 App 私钥，
   不复制 controler 的广泛权限，也不配置个人 PAT。复用现有 `AssignmentSecret.push`。
2. 每次编码执行准备临时 `gh` 包装和独立配置目录，放在仓库与个人配置目录之外；
   用当前 token 注入 `GH_TOKEN`，设置 `GH_CONFIG_DIR`、`GH_HOST=github.com`、
   `GH_REPO=<当前仓库>`、`GH_PROMPT_DISABLED=1`，并前置包装的 PATH。
   start、resume、fallback 及其 shell 子进程都使用本轮环境，不复用历史会话的凭据或路径。
3. 同时在受管子进程环境设置 bot token 和独立配置，避免普通绝对路径 gh 调用绕开 PATH
   包装后读到个人登录态。继承的 GitHub/Enterprise token、gh 配置和调试变量不带入执行环境。
   不执行 `gh auth login` 或 `gh auth setup-git`，不修改用户的全局 gh/Git 配置。
4. **默认不扩权：** 当前 token 保持 `contents: write`。需要 PR、Issues 等额外权限的 gh
   操作明确失败，不能回退到个人账号；“使用 bot 身份”不表示所有 gh 子命令都获得授权。
   PR 继续通过 `flow.open_pr` 提交交付意图，由 controler 在结果校验后创建或复用，
   不改变其持久化、去重及未知写入结果的恢复合同。
5. token 缺失、仓库不匹配、过期或找不到真实 gh 时，准备阶段明确失败，不启动 agent。
   包装在每次调用前检查有效期；运行中到期或权限不足时保留失败，不自动重放可能已生效的写操作。
   本次不添加续期 broker；新 assignment 获取新 token。长任务超过 token 剩余寿命仍可能失败。

#### 技术设计与受影响模块

- `app/domain/models.py`、`app/worker/execution/executor.py`：让执行上下文携带完整、不可分割的
  类型化 capability（token、repository、expires_at），替换仅有裸 `push_token` 的上下文。
  数据由现有 `PushSecret` 映射，不增加 wire 协议；Git credential helper 继续复用同一 token。
  INITIAL 在 seed 准备前检查 gh 可用性；REVISION 不因环境初始化重新 clone 或重排执行。
- `app/worker/execution/gh.py`（拟新增）：集中管理每次调用的包装、私有目录及环境，
  在前置 PATH 前解析真实 gh 的绝对路径，避免递归调用自身。
  包装通过 `exec` 和原样 argv 转发，不使用 eval；透传退出码、stdin、stdout、stderr。
  token 只通过短期环境提供，不写入包装脚本、命令参数、URL、配置、日志或 worker journal。
- `app/worker/execution/codex.py`：在 start/resume 共用的进程入口接入该环境，
  保留现有进程组回收；成功、非零退出、超时、取消及启动失败都清理本轮私有目录。
  先等待进程组回收，再删除环境。输出的 `last_message` 和 `failure_detail` 都脱敏当前 token，
  不把 gh 的错误输出变成泄漏路径。fallback 每次创建新环境，不复用失效包装。
- `Dockerfile`、部署说明：worker 镜像增加 gh CLI，serve 镜像不增加；明确宿主 worker 也须安装 gh。
  现有 worker 镜像尚未安装 gh，新检查不能只在开发机成立。
  凭据组件保持后端无关；本次不顺带实现仍处于 Planning 的 Amp adapter。

#### 备选方案、风险与取舍

- 持久 `gh auth login` 或把 token 写入包装/配置会留下可复用凭据，不采用。
- 只添加 PATH 包装不能处理普通绝对路径调用，因此同时隔离 gh 配置并注入子进程认证环境。
- 只设全局 `GH_TOKEN` 缺少任务生命周期和过期检查，也会扩大凭据暴露范围，不采用。
- 自动续期服务、新的网络 API、worker PR/Issue 写权限都会扩大本次改动；本方案暂不引入。
- 包装和环境注入只约束受管执行路径，不能阻止任意全权限代码修改环境、读取 token 或另行执行程序；
  不将其描述为安全沙箱，继续要求专用账户、容器或 VM 隔离。
  也不把 `/user`、`gh api user` 或个人登录检测当作 installation 身份验证的依据。

#### 验证与消融

- 用离线 fake gh 通过实际子进程边界检查：从个人配置/继承 token 的干扰环境启动，
  INITIAL、resume、fallback 均得到当前 bot token、仓库和独立配置；普通绝对路径 gh 也继承该环境。
  通过代表性含空格/引号参数、三路 IO 和非零退出验证包装转发，不为每个 helper 单独保留测试。
- 覆盖缺失 gh、过期 capability 不启动 agent，以及运行前到期不执行真实 gh；
  验证成功和取消后的私有目录清理、进程组先退出、token 不落盘且返回结果脱敏。
  保留原有本地 fixture remote 的 clone/checkout 验证，不使用真实 token 或访问真实仓库写接口。
- 消融只针对身份隔离这一关键机制：分别移除 bot token 注入或独立 `GH_CONFIG_DIR`，
  确认受个人凭据干扰的代表回归重新失败；恢复后重跑。其余简单参数映射不增加消融矩阵。
- 实施后运行原计划回归，并增加 `tests/test_codex_driver.py`、`tests/test_worker_tools.py`，
  执行 ruff、mypy、差异检查及 worker 镜像中 gh 可用性检查；尚未执行新功能验证。

**已执行基线：** 本轮主代理运行
`.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_vcs.py tests/test_worker_executor.py tests/test_codex_driver.py tests/test_worker_tools.py`，
结果 **27 passed in 2.97s**。本机 `gh version` 为 2.88.1；`gh help environment` 确认
`GH_TOKEN` 优先于已存凭据，且支持独立 `GH_CONFIG_DIR`。这些结果不证明尚未实现的包装可用。
计划草稿使用本地 Claude Opus 5.5 咨询，初始化实际模型为 `claude-opus-5-5`，
`tools=[]`、`mcp_servers=[]`，最终 `is_error=false`；主代理已核对并缩小测试及类型改动范围。

#### 待确认与交付状态

待用户批准“自动 clone + 每次执行的 bot gh 包装”整体方案。
若用户要求 worker 的 `gh pr`、`gh issue` 等也具备额外读写能力，需先明确所需权限，
以及是否改变 controler 的操作归属；本条默认只统一认证，不据此扩权。
各 gh 子命令在当前 scope 下的可用范围、agent shell 的实际继承行为及镜像构建仍待实施验证。
本轮仅追加此计划；未修改产品实现或测试，未提交、推送、创建 PR 或部署。

### [002] — 2026-10-20 — Git push 认证与 worker → controler → bot PR 交付详解

#### 要求与决策状态

用户要求在计划中详细说明 worker 如何 push、如何创建 PR，并先提交 plan PR。
本条补充 [001] 的认证和交付合同；发布文档已获授权，产品实现和新测试仍待计划批准。
不将 PR 创建搬到 worker，不扩大 worker 的 GitHub 权限，不重设计 workflow/coordinator。
计划以本轮只读 fetch 的 `origin/main` 为基线，单独发布此文档；不发布当前工作区的其他功能。

#### 操作、凭据与权限

| 操作 | 使用的身份/凭据 | 权限与执行方 | 当前或拟议行为 |
|---|---|---|---|
| 签发任务 GitHub token | controler 用 App 私钥生成的 App JWT | App installation 已授权的目标仓库 | 已有：签发单仓库 installation token |
| clone/fetch 目标仓库 | 当前任务的 installation token | Contents read；由 Contents write 涵盖 | 拟新增缺失/空 seed 的首次 clone，不自动刷新已有 seed |
| commit | 仓库生效的 Git author/committer 配置 | 本地操作，无需 GitHub token | 已有：agent 自主提交；不自动改写提交作者 |
| push Flow 分支 | 同一 token，经临时 Git credential helper | Contents write；worker 内的 agent 执行 | 已有凭据注入；拟补 helper 链隔离 |
| `gh` 调用 | 同一 token，经 `GH_TOKEN` 和临时包装 | 仅已签发权限；worker 内的 agent 执行 | 拟新增；超出权限失败，不借用个人账号 |
| 远端 ref 核验 | 同一 token，经隔离环境下的 `git ls-remote` | Contents read；worker 执行 | 已有核验；拟补 INITIAL 的 token 绑定 |
| 本地 MCP `open_pr` | 本轮独立 MCP token | 只保护本地工具入口，不是 GitHub 权限 | 已有：仅允许提交标题、正文和核验后的 VCS 事实 |
| RPC `flow.open_pr` | worker 连接认证及当前 assignment 身份 | controler 校验 worker/job/epoch/租约 | 已有：复核远端 SHA 并持久化交付意图 |
| 查找/创建 PR | controler 自己的 App installation 认证 | 创建需 Pull requests write | 已有：通过终态校验后由 controler 创建或复用 |

签发与校验见 [`Repository.create_push_capability`](../../app/github/repositories.py)、
[`JobCoordinator._assignment_secret`](../../app/controler/workflow/coordinator.py) 及
[`PushSecret`](../../app/contracts/protocol.py)。现有 `origin/main` 已接受 GitHub 隐含的
`metadata: read`，它不增加写权限；本功能保留单仓库和其他权限拒绝规则，不回退这项修复。
安装凭据不能超出 App 已获授权的仓库和权限，通常一小时后过期；
参见 [GitHub installation 认证](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)。
PR 创建需要的权限见 [GitHub Pull requests 权限表](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps#repository-permissions-for-pull-requests)。

#### Git push 如何使用 bot 认证

当前 [`git_credential_environment`](../../app/worker/execution/credentials.py) 为 Git 注入
`x-access-token` 用户名和从 `$FLOWTOOL_PUSH_TOKEN` 读取密码的 helper；
[`CodexAgent`](../../app/worker/execution/codex.py) 将其加入 start/resume 子进程的环境。
因此 `git push` 不需要通过 gh，也不依赖 `gh auth login`；仅设置 `GH_TOKEN` 不会替代 Git helper。
自动 clone 使用不含凭据的 `https://github.com/<owner>/<repo>.git`，agent 向任务规定的 remote 和
Flow 分支 push。token 限定仓库，并未限定到某个分支；任务分支约束和事后核验不能当作分支级权限隔离。
Contents write 不保证绕过分支保护、rulesets 或需要额外权限的操作，本方案不新增自动 force-push。
Git/API 认证身份与 commit 的 author/committer 分开，不据此修改作者。

**新增离线发现：** 当前 helper 是追加到已有链中，仍可能先取得个人或预置 helper 返回的凭据。
本轮用 `git config --file` 建立私有 fixture：全局 `credential.helper` 返回合成的 inherited 凭据；
将该文件设为 `GIT_CONFIG_GLOBAL`，关闭系统配置，再叠加现有 helper 环境运行真实的
`git credential fill`（目标 `https://github.com/owner/repo.git`）。结果选中了 inherited 凭据。
在 assignment helper 前增加空值的 URL 级 helper 后，同一命令只返回 assignment 合成凭据。
这是离线凭据解析证据，未使用真实 token，也没有观察或执行真实 GitHub push。

**拟议修正：** 统一 Git 环境必须先清空继承的 helper 链，再注入当前 capability 的 helper；
clone、agent push、远端 ref 校验都使用这条规则，不以个人凭据兜底，不写持久凭据文件。
保留 Git username 配置，token 不进入 remote URL、argv、日志或结果；helper 的凭据来源只限本轮环境。
同时核对 [`_git_args`](../../app/worker/execution/vcs.py) 的命令级 `credential.helper=`：
它适合本地元数据命令，但不能在需要认证的 clone/`ls-remote` 中最后清掉任务 helper。
认证命令使用既有隔离执行方式，按实际参数和环境顺序验证，不再添加一套全局认证配置。

#### INITIAL 的远端核验也必须携带当前 token

源码目前仅把 INITIAL token 交给 `CodingExecutionContext`，而创建给 WorkerMCP 使用的
`VcsExecutionContext` 时没有绑定其 `push_token`；该字段默认为 `None`。
`_verify_git`、`_verify_jj` 会将它交给 `_remote_sha`，因此私有仓库的 INITIAL 校验存在无法认证的风险。
REVISION 的 `prepare_revision_context(..., secret.push.token)` 已有对应绑定。
依据见 [`WorkerExecutor._initial/_revision`](../../app/worker/execution/executor.py) 和
[`VcsExecutionContext`](../../app/domain/models.py)。这是代码路径分析，未做真实私有仓库失败复现。

实施时将同一个当前 capability 贯通到 clone、编码进程、INITIAL 的 MCP 前置核验和执行后的最终核验。
Git/jj 本地事实采集无需联网；认证只在远端读取/push 时使用。
远端 ref 校验继续在仓库之外的临时 cwd、无个人 Git 全局/系统配置的环境中查询已固定的 URL，
要求本地 commit SHA 与远端 Flow 分支 SHA 相同；不接受 agent 改写 remote 后的自报结果。
此处补足私有仓库准备与交付所需的认证，不改变现有独立核验合同。

#### 完整交付时序

1. **controler 派发：** 为当前 INITIAL 签发新 capability，经 wire-only `AssignmentSecret.push`
   下发 token、repository、expires_at；不把 App 私钥交给 worker，也不把 token 保存进 journal。
2. **worker 准备：** 校验任务身份、受管路径、token 的仓库/有效期及 gh 可用性。
   已有 Flow 副本直接复用；否则只对缺失/空 seed 执行原子 clone，按任务配置基线和 remote 后复制 Flow。
   已有有效 seed 不 fetch、reset 或改写其 remote；SSH remote 不在现有核验支持范围内，明确提示修正，
   不偷偷改写用户的预置仓库。clone 失败、超时或取消不发布 seed、不启动 agent。
3. **worker 准备本轮认证环境：** Git helper 先重置再注入；gh 包装前置 PATH，并设置 bot `GH_TOKEN`
   和空的独立 `GH_CONFIG_DIR`。MCP 使用另一份本轮专属 token，只开放 `open_pr`。
4. **agent 编码与推送：** 在 Flow 副本中选择原生 Git/jj，创建任务指定的分支/bookmark，提交并 push。
   worker 不替 agent 自动提交或 push，也不把安装项目依赖纳入仓库初始化。
5. **agent 请求 PR：** 调用本地 MCP `open_pr(title, body)`。worker 先独立验证远端结果，
   再通过活动 assignment 的 caller 发出 `flow.open_pr`，携带核验后的分支和 SHA。
6. **controler 准备交付：** 校验 assignment、worker/epoch、当前 Flow/Attempt、分支与 SHA，
   再用自己的 installation 认证读取 GitHub ref。远端读取后在事务内复核状态，
   保存标题、正文、head SHA 和 OPENING 状态，返回 `PreparedDelivery(status="prepared")`。
   **这个回执表示意图已保存，不表示 PR 已创建，也不保证已有 PR URL。**
7. **worker 返回 INITIAL 结果：** agent 必须成功退出且输出严格符合 schema，VCS 最终核验通过，
   `open_pr` 调用已完成，且调用时与最终 VCS 事实一致。结果包含调用 ID 和交付回执。
   只有 push 没有完成工具调用，或者工具调用后又改变交付 SHA，都不能当作 INITIAL 成功。
8. **controler 实际创建：** 接收并校验 INITIAL 终态后，持久操作执行器按分支查找已有 PR，
   没有且可确认未执行过创建时再调用 GitHub 创建接口；创建使用 controler 的安装凭据，
   不使用 worker 的 Contents-only token。后续标签、reviewer 及交付绑定沿用现有流程。

```diagram
┌──────────────────────────┐
│ controler 签发本轮 token  │
└────────────┬─────────────┘
             ▼
┌──────────────────────────┐
│ worker 初始化仓库和认证   │
│ agent commit / push      │
└────────────┬─────────────┘
             │ MCP open_pr → RPC flow.open_pr
             ▼
┌──────────────────────────┐
│ controler 复核 SHA        │
│ 保存意图，返回 prepared   │
└────────────┬─────────────┘
             │ worker 成功结束，INITIAL 结果校验通过
             ▼
┌──────────────────────────┐
│ controler 用 bot 凭据     │
│ 查找 / 创建并绑定 PR      │
└──────────────────────────┘
```

源码依据：[`WorkerMCP`](../../app/worker/transport/mcp.py)、
[`FlowToolService._open_pr`](../../app/controler/workflow/tools.py)、
[`WorkerExecutor._initial`](../../app/worker/execution/executor.py)、
[`initial_delivery_mismatch`](../../app/controler/workflow/initial.py) 及
[`PullRequestEffect` 执行分支](../../app/controler/workflow/effects.py)。
保留“worker 发起、controler 创建”可使 assignment 校验、持久去重和创建结果恢复继续集中在 controler；
直接授权 worker 运行 `gh pr create` 会绕开这套时序，本次不采用。

#### 后续修订、错误与生命周期

- **REVISION：** 每个 assignment 获取新 token，先核对冻结的 expected head/remote，再恢复会话。
  resume 和经“尚未执行”证据允许的一次 fallback 都重建自己的 gh 环境，不复用 INITIAL 的 MCP。
  推送同一 PR 分支以更新已有 PR，不再创建 PR；未知执行、超时或可能已 push 均不触发自动 fallback。
- **ARCHIVE：** 只处理既有绑定工作区的受管清理，不 clone、不准备编码用 gh 环境。
  之后的 GitHub 标签/评论等回写仍由 controler 完成。
- **过期/权限不足：** 启动前拒绝无效 capability；gh 包装调用前检查有效期，Git 远端请求也受
  GitHub token 有效期约束。运行中到期或缺权限时明确失败，不自动续期或借用个人登录。
  gh 的 PR/Issue/Actions 等命令不因身份统一就自动获得权限；具体可用命令需按 scope 验证。
- **未知 PR 创建结果：** 先按固定分支复核；结果仍无法确认就保留待核对状态，不盲目重发创建。
  已存在但关闭的 PR 保留原身份并按既有规则暂挂，不自动回开或另建替代 PR。
- **退出/取消：** 子进程和进程组清理完成后销毁临时 gh 环境，返回结果脱敏；不承诺撤回已经发生的 push。
  删除环境也不等于立即撤销 GitHub 端 token。包装与环境隔离仍非 OS 沙箱，外部账户/容器/VM 隔离不变。

#### 验收、证据与实施边界

- **保留一条 helper 优先级回归：** 使用上述冲突全局配置和合成凭据运行真实 `git credential fill`，
  断言只得到 assignment 凭据；临时移除链重置后回归必须失败。覆盖认证参数不能清掉任务 helper。
- **贯通 token 的合同：** 私有认证替身在 clone、MCP 远端校验、INITIAL 最终核验及 REVISION 中只接受
  当前 token；用本地 bare remote 实际执行 clone/checkout/push/`ls-remote`，比较本地和远端 SHA。
  本地 remote 验证 Git 行为，不能冒充真实 GitHub installation 权限验证。
- **PR 创建边界：** 更新/复用已有交付合同测试，确认 MCP prepared 后尚不调用 PR 创建，
  非法/失败的 INITIAL 不创建 PR，通过终态校验后才进入 create-or-reuse；不为每个内部函数添加测试。
- **gh 与准备边界：** 沿用 [001] 的干扰凭据、参数转发、过期拒绝、取消清理和 resume/fallback 验证；
  缺失/空 seed 成功，已有 seed/Flow 保留，失败不启动 agent。worker 镜像也须检查 gh 实际可用。
- **已运行基线：**
  `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_vcs.py tests/test_worker_executor.py tests/test_codex_driver.py tests/test_worker_tools.py tests/test_github_client.py`
  结果为 **60 passed in 3.06s**；这是当前本地代码的现有测试基线，不证明新功能已实现。
- **咨询与限制：** 本条草稿使用实际模型 `claude-opus-5-5` 的本地只读咨询，初始化无 tools/MCP，
  最终 `is_error=false`；主代理核对源码和证据后补全身份区分并修正文稿。
  尚未新增产品或测试代码，也未做真实私有仓库 clone、push 或应用 PR API 写入验证。

本次发布范围仅为这个 feature record；PR 保持 draft，待用户批准整体方案后才在同一 bookmark/PR 实施。
实现前还需核实实际 installation 的 PR 权限、worker 镜像中的 gh 版本与 agent shell 的环境继承；
这些检查不能被本文的计划表述或已有测试基线替代。

### [003] — 2026-10-20 — 优先复用副本，全部占用时增加 Git worktree

#### 需求、取代范围与状态

用户要求：“优先复用副本仓库，当都被占用就用 git worktree 加一个执行位置”，
主要目的是“限制依赖和构造文件无限制增长”。本条按“构建产物”理解“构造文件”。
以下是待批准的方案，未修改产品实现或测试。

本条取代初始计划及 [002] 中“每个新 Flow 完整复制 seed”和“归档总是删除执行副本”的决定：
Flow 独占一个可复用执行位置，同一 Flow 的重试、修订仍绑定原路径；安全释放后位置可供新 Flow 使用。
自动准备缺失 seed、每次执行的 bot gh 包装、Git helper 隔离、短期能力及 controler 创建 PR 的边界不变。
已有用户目录不自动纳入池，不为此增加旧目录迁移或兼容层。

现状依据：[`prepare_flow_repo`](../../app/worker/execution/vcs.py#L83-L111)
当前每个 Flow 执行 `copytree`，包括 seed 内的依赖与产物；
[`WorkerExecutor`](../../app/worker/execution/executor.py#L88-L112) 在归档时删除该目录。

#### 拟议分配与释放规则

1. **先找原绑定。** 当前 Flow 已持有执行位置时直接使用它；不能因重试或新 assignment
   改换目录，也不能在 REVISION 缺失目录时重新 clone 掩盖进度丢失。
2. **优先复用空闲副本。** 查找当前仓库已安全释放的受管完整副本；使用稳定的位置 ID，
   不再按 `flow_id` 新建完整副本。首次没有副本时才建立保留副本，之后不按 Flow 增加完整复制。
3. **全部占用时增加 worktree。** 从同仓库的受管 Git seed 创建独立 checkout，
   不复制其他 Flow 的工作树、`.git` 文件或 `.jj`。worktree 使用独立目录和任务分支，
   创建前持久记录归属，失败或取消只回收本轮已证明拥有的残留。
   此操作会登记 seed 的 Git worktree 元数据，但不改写 seed 的 checkout、remote 或未提交内容。
4. **同 Flow 持有至安全释放。** 执行中、等待 review、断线恢复或执行结果未知的位置都算占用；
   job 结束、租约过期或数据库绑定释放，均不能单独证明目录可以重用。
5. **保留副本归还池，溢出位置移除。** 成功归档释放完整副本，保留允许复用的依赖和构建缓存；
   临时 worktree 经归属和状态核验后通过 `git worktree remove` 移除，连同其本地依赖和产物释放。
   不用直接 `rmtree` 代替 worktree 注销，也不执行会影响其他位置的无差别 prune。
6. **异常内容不覆盖。** 完整副本有未提交的 tracked 修改、非 ignored 的未知文件、
   进行中的 VCS 操作或归属不明时，隔离并报告，不自动 reset/clean 后当作空闲。
   清理失败保留原归属和恢复证据，不虚报释放成功。

```diagram
┌──────────────────────────┐
│ INITIAL：查找 Flow 原绑定 │
└────────────┬─────────────┘
             │ 没有原绑定
             ▼
┌──────────────────────────┐    有    ┌──────────────────┐
│ 有已安全释放的副本？      │────────→│ 复用原目录和缓存 │
└────────────┬─────────────┘          └──────────────────┘
             │ 全部占用，且未达到上限
             ▼
┌──────────────────────────┐
│ 新增临时 Git worktree     │
│ 安全释放后移除目录和产物  │
└──────────────────────────┘
```

#### 磁盘增长与缓存边界

- Git worktree 共享 Git 对象库，**不共享** `node_modules`、`.venv` 或构建目录；
  不能把 worktree 当作依赖去重机制。保留池减少重复安装和按历史 Flow 累积的目录，
  临时 worktree 释放即清理，避免保留“历史并发峰值”留下的所有缓存。
- 被 ignore 的缓存只在保留副本内复用，不跨并发位置用 symlink 共享可写依赖或构建目录。
  复用时保留项目 ignore 的缓存，tracked 状态必须干净，基线切换只能针对已证明空闲的受管副本。
  对齐 seed 已提交基线时，若有未提交源状态或缓存路径会被覆盖，明确拒绝，不静默丢弃。
- 不执行通用 `git clean -fdx` 来复用目录，它会清掉依赖和产物；
  `git clean -fd` 会保留 ignored 内容，但本方案也不据此自动删除未知的非 ignored 文件。
  缓存是否适配当前 lockfile、解释器或工具链，仍由项目正常的依赖同步和构建流程保证；
  worker 不自动安装依赖，也不承诺保留旧缓存就没有污染。
- 为避免被占用、失败或状态未知的位置持续触发扩容，需要明确**每仓库执行位置数量上限**。
  已占用、准备中、清理中及隔离的位置均计入；到上限时报告容量不足，不启动 agent，
  不驱逐活跃位置、不回退为完整复制。具体上限待确认。
- 位置数量上限不等于字节配额：单个缓存和 Git 对象库仍可能增长，仓库种类也可能增加。
  全局磁盘配额、按字节淘汰和后台 GC 不在本条默认范围内，不能据此宣称总磁盘有硬上界。

#### 所有权、恢复与现有调度约束

- 增加 worker 本地执行位置清单，持久记录仓库、worker/root 身份、位置 ID、受管相对路径、
  类型（完整副本或 worktree）、持有 Flow、分配代次和状态。新 Flow 使用同一位置时递增代次。
  在现有 root 锁内原子更新，清单损坏、位置异常或归属不明时拒绝自动接管。
- [`WorkerSession`](../../app/worker/state/session.py#L131-L198) 的单条 assignment journal
  不能代替位置清单。journal 绑定本次位置 ID 和代次，任何释放先持久保存证据；
  只有 Flow、root、位置与代次都匹配才能修改目录。旧归档重试不得释放已借给新 Flow 的位置，
  结果重放和 ACK 沿用原有“已完成则只重放、不再执行”语义。
- [`ArchiveResult.cleanup`](../../app/contracts/protocol.py#L186-L188) 当前只有
  `removed | already_absent`。实施时增加表示“已释放但目录保留”的明确结果，并贯通 worker、
  controler 的归档核验、状态展示和测试；不能目录还在却报告已经删除。未上线代码不保留兼容层。
- 当前 [`flow_executions` 约束](../../app/persistence/schema.py#L89-L100) 及
  [`assign_job`](../../app/persistence/workers.py#L178-L259) 在 Flow 生命周期独占 worker，
  等待 review 不会让同 worker 接第二条 Flow。**本条不修改这些调度约束。**
  worker-local 池下，正常的顺序 Flow 主要走副本复用；旧位置仍需保留、但 worker 已被释放给
  新 Flow 时才可能需要溢出。测试可显式构造全占用，不能把它当作生产已支持同 worker 并发。
- 建议先限定每个 worker root 内复用，避免跨 OS 身份共享可写目录和凭据隔离风险。
  若要求同一主机多个 worker 共用副本，需明确增加独立共享目录、跨进程锁及所有权边界；
  不能让多个 worker 共用现有 `FLOWTOOL_WORKER_ROOT` 绕过 root 锁。

#### Git/jj、模块与实施顺序

完整副本继续保留现有 Git/jj 支持；复用时需通过对应 VCS 校验和恢复已提交基线。
临时 Git worktree 默认只提供 Git，不复制 `.jj` 或把 jj-only 仓库静默转换成 Git；
没有可用 Git seed 时明确失败。真实 jj 与 linked worktree 的组合未经验证前不作支持声明。

1. `app/worker/execution/workspaces.py`（拟新增）：集中负责位置清单、分配、查找、容量及释放；
   这是独立的持久所有权职责，不把池状态混入 VCS 结果验证。
2. `app/worker/execution/vcs.py`：以一次性保留副本准备、空闲状态核验和 worktree 生命周期替换
   每 Flow 的 `copytree`/路径计算/删除；保留受管路径、symlink 与取消后进程组回收约束。
3. `executor.py`、`state/session.py`、归档结果契约及 controler 归档核验：INITIAL 分配，
   REVISION 查原绑定，ARCHIVE 按代次释放；移除被取代的旧接口，不保留双套目录策略。
4. 更新相关合同测试、README 和部署说明；不混入工作区现有的 UI、标签、更新部署等其他功能。

备选方案：继续每 Flow 完整复制不能满足本次复用要求；全部改用 worktree 会失去优先复用
温热副本的行为；跨位置共享可写依赖目录会引入相互影响。因此采用“保留副本 + 临时溢出”，
成本是新增位置所有权清单、容量约束和释放结果；不扩大为通用调度或磁盘管理服务。

#### 验证与消融

- **拟验收合同：** 本地 Git fixture 上连续多条 Flow 复用同一目录和允许保留的缓存；
  同 Flow 重试/修订路径不变；所有位置占用时真实创建 worktree，释放后 Git 登记和目录消失；
  达到上限不扩容、不启动 agent，活跃、未知或隔离位置的内容不变。
- **拟验收恢复：** 覆盖分配/释放中断、目录缺失但无证据、清单损坏和 symlink 越界；
  旧归档在位置被新 Flow 借用后重放，不得清理新 owner；原 worker 重启、结果重放和 ACK 不重复释放。
  通过现有 executor/session/archive 外部合同检查，不为每个内部 helper 增加永久测试。
- **消融：** 实施后临时移除空闲复用，确认固定工作负载下位置创建/依赖初始化次数增加；
  临时移除归属代次校验，确认迟到归档保护用例失败；恢复后重跑相关检查。
  位置删除可能掩盖峰值增长，因此同时测创建次数与峰值目录/字节数，不只比较最终 `du`。
- **已执行现状探针：** 真实 Git worktree 与 seed 指向同一 Git common dir，新 worktree
  不带 seed 的 `.venv`/`build`；`git clean -fd` 删除非 ignored 文件、保留 ignored 缓存，
  `git worktree remove` 可移除干净的临时位置。
  [本线程探针记录](https://ampcode.com/threads/T-01a15a9b-ba3f-763b-a7e1-37e4d1a4eefd)，
  本地日志为 `.amp/in/artifacts/worker-workspace-pool-probe.log`。
  这不证明尚未实现的池或容量限制有效。
- **已执行基线：** 初次未配置测试数据库时 26 passed、10 skipped；按现有本地 PostgreSQL
  测试配置重新运行
  `FLOWTOOL_TEST_DATABASE_URL=postgresql://localhost/flowtool_test .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_vcs.py tests/test_worker_executor.py tests/test_workspace_archive.py tests/test_archive_worker_recovery.py tests/test_worker_session.py`，
  **36 passed in 6.73s、无跳过**。
  [本线程测试记录](https://ampcode.com/threads/T-01a15a9b-ba3f-763b-a7e1-37e4d1a4eefd)，
  本地日志为 `.amp/in/artifacts/worker-workspace-pool-baseline-postgres.log`。新功能测试待实施。
- **计划咨询：** 草稿使用本地 Claude Code 的实际 `claude-opus-5-5`，初始化
  `tools=[]`、`mcp_servers=[]`，最终 `is_error=false`；主代理核对并简化方案，
  未采用额外 bare 仓库等扩展，补入实际探针和测试证据。
  [本线程咨询记录](https://ampcode.com/threads/T-01a15a9b-ba3f-763b-a7e1-37e4d1a4eefd)，
  本地元信息为 `.amp/in/artifacts/worker-workspace-pool-consultation.json`；咨询不替代运行验证。

#### 待确认与交付状态

实施前需确认：池仅在每个 worker 内复用，还是要求同主机跨 worker 共享；
每仓库最多保留/允许多少执行位置。建议每 worker、每仓库保留一个温热完整副本，
溢出 worktree 不长期保留；总位置上限不擅自设为已批准的数字。

本轮仅在原 feature record 追加 [003]，未修改产品实现或测试，未提交、推送、更新 PR 或部署。
后续在同一 `fix/worker-repo-bootstrap` bookmark / PR #103 继续，待用户确认范围并批准方案后实施。

### [004] — 按 worker 隔离，每仓库位置上限默认 2 且可配置

#### 需求与决策状态

用户确认：“按worker独立，每个仓库先2个，这个做成配置项”。
本条解决 [003] 的池范围和数量上限两个开放问题；这两项需求已确认，产品实现仍待整体计划批准。
“2 个”按前一轮所问的每仓库**执行位置总数上限**记录，包含完整副本和临时 worktree，
不表示启动时预建两个完整副本，也不表示一个副本之外还可以再建两个 worktree。

#### 配置与容量合同

- **隔离范围：** 每个 `FLOWTOOL_WORKER_ROOT` 独立保存执行位置及清单，按仓库分别计数；
  不新增跨 worker 共享池，不改变 Flow 生命周期独占 worker 的调度合同。
- **配置项（拟新增）：** `FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT=2`，
  对应 `WorkerSettings.worker_repo_workspace_limit`，类型为整数、默认 `2`、最小 `1`。
  沿用现有 `FLOWTOOL_` 进程环境加载方式；非法值在启动时拒绝，不静默回退默认值。
  一个 worker 上所有仓库使用这个上限值，但各仓库独立计数，不增加逐仓库配置映射。
- **计数：** 完整副本、临时 worktree，以及准备中、已占用、清理中或隔离的位置都计入；
  只作为模板、不供 Flow 编码的 seed 不计入执行位置数量。
  已有同 Flow 绑定优先复用，其次借用可用的空闲副本；只有新建位置才需要额外容量。
- **默认行为：** 首次需要时建立一个可长期复用的完整副本；它被占用且需要另一个位置时，
  最多增加一个临时 worktree。完整副本安全释放后归还池；临时 worktree 安全释放后移除。
  第三个位置的准备请求返回容量不足，不启动 agent、不追加完整复制、不驱逐已有位置。
- **配置生效：** 启动时读取，不新增热更新服务。重启时若新上限低于已有位置数，
  保留已有归属与恢复证据，允许原 Flow 继续使用和安全释放；在数量回到新上限以下前禁止新增位置。
  不通过降低配置来自动删除既有目录，也不扩大为按字节配额或后台 GC。

#### 实施与验收补充

- 在 [003] 的执行位置分配方案中接入该配置，`WorkerRuntime` 将已验证的上限传给分配组件；
  不让分配组件另读环境变量，worker/control 的边界保持不变。
- 在既有配置测试中验证默认值、环境覆盖、非法值拒绝；通过实际位置分配合同验证默认容量内
  复用/扩容、第三个位置拒绝、释放后恢复容量，以及上限降低时不破坏已占用位置。
  不为简单参数传递单独增加永久测试。
- 部署说明同步记录配置名、默认值、计数范围和重启生效；其他 [003] 的缓存、所有权、
  Git/jj、重放及归档验收要求不变。

本轮仅更新计划，没有修改 `WorkerSettings`、运行实现或测试，未提交、推送、更新 PR 或部署。

### [005] — 2026-10-21 — 实施自动准备、bot 环境与有界执行位置池

#### 已实现的行为与取舍

- `execution/workspaces.py` 持有单仓库位置清单，首次完整复制，后续优先复用；全部占用且
  未达上限时创建 Git worktree。`WorkerSettings.worker_repo_workspace_limit` 默认 2、最小 1，
  runtime 显式传入。seed 不计数；低于现有数量的配置不驱逐原绑定。
- 清单记录 worker/root 的路径及 device/inode、仓库、稳定位置 ID、相对路径、Flow、代次、
  类型和状态；root 锁下原子替换并 fsync。journal v4 记录相同绑定，释放前持久化证明。
  准备中断或清单/路径异常拒绝接管，已完成结果沿用原有 replay/ACK。
  完整副本 `released` 后保留目录；临时 worktree 用 `git worktree remove --force` 注销，
  先核验干净状态和 common dir/登记归属，force 仅用于允许删除 ignored 缓存。
  失败保留 `releasing` 证据；迟到归档的代次不匹配会失败，不能操作新的 owner。
- `execution/vcs.py` 拒绝向祖先发现仓库，缺失/空 seed 使用干净 HTTPS URL 和当前 token
  clone 到同文件系统临时目录，确认 HEAD 后 rename；失败/取消删除本轮临时 clone。
  文件复制线程在取消后仍等待结束，清掉仅本轮新建的未交付副本。既有 seed 不 fetch/reset。
  空闲副本从 seed 已提交基线对齐；Git 使用 `--no-overwrite-ignore`，jj 在切换前检查缓存冲突。
  不丢弃 tracked 修改、未知文件或未完成操作；不提供旧目录迁移、字节配额或后台回收。
- `GitHubCapability` 将 repository/token/expiry 一起传入编码上下文；每次 start/resume/fallback
  生成新的私有 gh 包装/空配置，固定 host/repo、禁交互，执行前检查期限，再 exec 原样 argv。
  同时注入子进程 bot 环境，普通绝对路径 gh 也使用当前身份；它不经包装期限检查，GitHub
  仍执行 token 有效期校验。保持 Contents-only 权限，无 PAT、App 私钥或续期 broker。
- Git helper 先清空继承链，再提供当前 installation token。INITIAL 从 clone 到 MCP 和
  最终远端核验贯通同一 token。Codex 先回收进程组再销毁本轮目录，输出和错误都脱敏。
  PR 继续由 controler 接收 prepared 意图并在 INITIAL 通过校验后创建/复用；未更改操作归属。
- `ArchiveResult.cleanup` 增加 `released`，controler 状态文本明确目录保留；protocol v14
  拒绝旧协议，journal v4 不自动迁移。升级须先收敛旧任务/归档及 ACK，不能静默接管旧目录。
  `Dockerfile` 仅 worker stage 安装 gh，并在镜像构建步骤执行 `gh --version`。

#### 自查补充与验证

- jj 对齐通过私有 `refs/remotes/flowtool-seed/baseline` 导入已提交对象；单独 Git fetch
  不会让 jj 索引看见新提交。真实 colocated/non-colocated fixture 均验证更新后的 seed 基线。
  路径清单改为 NUL 分隔，检查目标及其祖先，拒绝覆盖含引号/换行的缓存和 symlink/file 祖先。
- 不接管在 `absent` 位置重新出现的目录。文件复制/清理即使收到重复取消，也等线程结束后
  才传播取消、回收本轮内容和释放 root 锁。补充公共分配/释放合同回归。
- 交付快照基于当时 `main@origin`，只包含本功能；共享工作区的 Amp history/protocol v15、
  UI、标签和 server-update 改动不带入。此快照使用 protocol v14、journal v4。
- 在隔离快照执行 `FLOWTOOL_TEST_DATABASE_URL=<本机 PostgreSQL URL> python -m pytest -q
  -p no:cacheprovider`：**895 passed，0 skipped，90.72 秒**。覆盖真实 Git clone/push/远端 SHA、
  bot gh start/resume、revision fallback、容量/温热复用、Git/jj 缓存保护，以及完整副本保留与
  worktree 移除的 8 种归档/重启/结果/ACK 窗口。仅有既存 Starlette TestClient 弃用警告。
- `ruff check app tests`、`mypy app tests` 通过（199 个源文件）；
  `python scripts/build_console.py`、`uv build --no-sources` 通过，wheel 包含新 worker 模块及
  控制台静态入口。Vite 既存的超过 500 kB chunk 警告不影响构建。
  本机使用 macOS arm64/Python 3.13；CI 的 Linux/Python 3.12 结果须单独确认。

#### 已执行消融与成本对照

临时实验只在子进程内 monkeypatch，一个变体只移除一个机制，不改生产文件。
同一隔离快照先运行下列合同的原版：6 passed；随后分别重跑对应合同：

| 移除的机制 | 对应 pytest 合同 | 实际结果及决定 |
| --- | --- | --- |
| `ensure_seed` | `test_bootstrap_clones_atomically_with_current_token_and_task_baseline` | 缺失/空 seed 的 2 例失败，保留原子准备 |
| Git helper 链重置 | `test_git_uses_assignment_even_with_a_personal_helper` | 1 例失败，实际返回 personal/inherited 合成凭据，保留重置 |
| bot `GH_TOKEN` 注入 | `test_real_agent_process_inherits_bot_gh_and_discards_invocation_environment` | start/resume 的 2 例失败，保留当前身份注入 |
| 独立 `GH_CONFIG_DIR` | 同上 | start/resume 的 2 例失败，保留配置隔离 |
| 分配代次核验 | `test_pool_reuses_warm_copy_bounds_overflow_and_fences_late_archive` | 1 例失败，旧归档错误释放同 Flow 的新代次，保留 fence |

可用 `python -m pytest -q -p no:cacheprovider tests/test_worker_workspaces.py
tests/test_worker_github_environment.py` 重跑正常合同。变体的具体设置分别为：seed 准备 no-op；
helper 直接追加而不清空；从准备环境删除一个 GH 键；所有权比较忽略 generation。
临时实验日志保存在本地 `.amp/in/artifacts/worker-bootstrap-ablation-*.log`，
汇总为 `worker-bootstrap-ablation.json`，不纳入版本控制。

额外用同一基线、4 个顺序 Flow、每次缺缓存时写入 1 MiB ignored payload 比较温热复用与
“每次 release 后删完整副本并将位置标记 absent”。直接计数 seed→copy 的 `copytree` 调用，
每轮 acquire 后采样位置数与实际文件字节；两组都执行正常归档检查：

| 指标 | 温热复用 | 每次删除 |
| --- | ---: | ---: |
| 完整复制 / 缓存初始化次数 | 1 / 1 | 4 / 4 |
| 累计缓存写入 | 1,048,576 bytes | 4,194,304 bytes |
| 执行位置峰值 / 文件字节峰值 | 1 / 1,077,171 bytes | 1 / 1,077,171 bytes |
| 全部归档后保留的缓存 | 1,048,576 bytes | 0 bytes |

保留复用是以空闲磁盘换取少 3 次完整复制和缓存初始化；顺序样本没有降低峰值磁盘。
并发容量由真实池合同另外验证：2 个位置可用，第 3 个拒绝；本实验不证明字节总量有界。

#### 仍待环境验收的边界

gh 使用离线 fake executable，不调用真实编码模型；Git fixture 只写本地 bare remote。
本机没有 Docker/Podman/容器运行时，无法执行镜像构建与镜像内 gh smoke；也未使用真实
installation token 做 GitHub clone/push/PR 写入验收。PR 保持 draft；独立审查通过后才可
更新同一 bookmark，远端 CI、真实 App/镜像 smoke 与部署状态以实际结果为准。
