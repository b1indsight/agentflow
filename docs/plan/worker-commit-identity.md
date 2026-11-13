# Worker bot 提交署名 — Feature record

当前实现按用户的新方向收窄：首次编码准备时只在 workspace 的 Git 配置中设置当前 App 的 bot，后续提交检查署名，将覆盖前的账号保留为标准 `Co-authored-by`。用户已确认参考为 `gh-bot`；初始方案作为历史保留，下文 [001]–[005] 记录后续要求与实际实现。

**Stage:** 原 worker 署名功能已合并；[005] 的 App Bot 身份来源修复已完成本地实现与验证，进入 PR 审查，尚未部署。
**PR:** 原实现为已合并的 [#150](https://github.com/b1indsight/agentflow/pull/150)；[005] 后续修复使用 `fix/app-bot-identity` → `main`，不重新发布原实现。
**基线:** 原实现基于 `9811f500dd11`；[005] 发布基于只读 fetch 后的 `main@origin`（`c4ee5ebee5a3`），原共享工作区及其并行改动保留。

## 1. Initial plan

### 动机、目标与范围

- 用户要求“把 worker 提交署名也改成 bot”。现有 worker 只注入 push/gh 认证，提交作者由本地配置决定；见 [`credentials.py`](../../app/worker/execution/credentials.py)、[`github_environment`](../../app/worker/execution/gh.py) 及 [现有部署说明](../deployment/README.md)。
- 编码任务创建的提交，其 Author 和 Committer 都应是配置的 GitHub App bot，覆盖 Codex/Amp 首轮、修订、恢复和 session fallback。
- 不增加 worker 身份配置，不改变 token 权限、agent 自主 commit/push 的分工和合法的 coauthor trailer。不回溯修正历史提交，不加入加密签名或 Verified 标识。
- 无 GitHub 编码上下文的调用及独立 inference 不注入 bot 署名。

### 建议行为

- 名称使用 `ServerSettings.bot_login` 对应的实际 bot login。邮箱使用 `{bot_user_id}+{bot_login}@users.noreply.github.com`；数值 ID 必须是 bot 用户 ID，不能使用 App ID。格式和查询方法见 [GitHub 官方 action 示例](https://github.com/actions/create-github-app-token/blob/main/README.md?plain=1)。
- control 通过 installation 认证查询 `GET /users/{bot_login}`，校验返回的 login 与配置一致（忽略大小写）、类型是 `Bot`、用户 ID 为正数。该接口支持 installation token 且无需新增权限，见 [GitHub REST 文档](https://docs.github.com/en/rest/users/users#get-a-user)。
- 查不到 bot、配置指向普通用户、返回身份不一致或请求失败时，任务准备失败并沿用现有错误分类/重试机制；不回退个人署名。
- 每轮启动重新注入身份，覆盖继承的个人 Author/Committer 以及 jj 身份。身份来源不依赖 agent 自行理解 prompt。

### 技术设计与职责

不新增身份框架或配置层，沿用现有 capability 类型和调用链。

1. [`PushCapability`](../../app/github/repositories.py)、[`PushSecret`](../../app/contracts/protocol.py)、[`GitHubCapability`](../../app/domain/models.py) 各增加两个必填字段：`bot_login: str` 表示已校验的 bot login；`bot_user_id: int` 表示对应的正数用户 ID。wire 边界校验非空 bot login 和正数 ID，不使用默认假身份兼容旧消息。
2. `Repository.create_push_capability(self) -> PushCapability` 保持签名，使用现有 installation SDK 查询并校验 bot，返回新增身份字段。初版不缓存，每次签发多一次只读查询。
3. `JobCoordinator._assignment_secret(...) -> AssignmentSecret | None` 与 worker 的 `_github_capability(...) -> GitHubCapability` 保持签名，传递这两个字段，不额外读取个人配置。
4. `github_environment(capability: GitHubCapability, directory: Path, source: Mapping[str, str]) -> dict[str, str]` 保持签名，在现有任务环境中增加 `GIT_AUTHOR_NAME/EMAIL`、`GIT_COMMITTER_NAME/EMAIL`、`JJ_USER`、`JJ_EMAIL`。Amp 和 Codex 已在 start/resume 共用此路径，不复制两套规则；Git credential helper 的职责和格式不变。
5. worker 的仓库准备路径处理既有 jj 工作提交，详见下一节。保留冻结远端 SHA、祖先、工作区绑定与只读 answer 的既有校验，不为更换署名放宽这些边界。
6. 更新 README 和部署说明中“提交作者仍由 worker Git 配置决定”的当前行为描述。旧计划文档保留历史原文，以本记录说明新要求。

### jj 的既有工作提交必须单独处理

本机 `jj 0.39.0` 的临时仓库探查已确认：先以个人身份建立 `@`，再用 bot 的 `JJ_USER/JJ_EMAIL` 执行 `jj commit`，生成的提交仍是个人 Author，只有 Committer 改为 bot。因此仅注入环境变量不能满足 Git/jj 一致署名的目标。

- 编码任务启动前，仅对属于当前 Flow、确认尚未发布的当前可变 `@` 更新作者，使用同一 bot 环境；保留内容、描述和 author timestamp。安装版本提供 `jj metaedit --update-author`，以实际支持版本的等价命令实施。
- 先完成原工作区和冻结交付基线的必要校验，再做局部身份准备；重新捕获本轮执行基线，不替换冻结的远端祖先锚点。
- 若当前提交已经属于远端历史，则保留它，在该基线上准备 bot 工作提交，不使用 `--ignore-immutable`，不改历史祖先，不 force-push。
- 只读 answer 不执行身份改写。Git 的普通新提交直接使用环境身份；不因这次功能自动 amend 已有提交。
- 这允许 worker 在准备阶段定向更新未发布 jj 工作提交的署名，是对原先“仅准备和核验”的有限调整；worker 仍不替 agent 选择提交内容、提交信息或交付分支，也不代为 push。

### 兼容、成本与替代方案

- 编码任务 wire 增加必填身份，建议协议从 22 升到 23，control/worker 配套升级。不做旧协议的个人身份回退。
- 实施前核对 assignment secret 的 journal/恢复路径，确认是否需要随同更新保存格式；目前不预设数据库迁移。升级前 drain，重启后领取带新身份的任务；实际部署和回滚须另获授权。
- 硬编码 `flowtool[bot]` 或 App ID 会导致其他部署错误归属；修改个人全局配置会影响 worker 外的操作；两者均不采用。
- 让 worker 查询 bot 会增加 worker 网络职责，仍需传递 login；选择 control 解析身份并随现有 capability 下发。
- 每次签发增加一次 GitHub 读取及一个准备阶段失败点，初版接受此成本，沿用已有请求超时和错误处理，不增加缓存和后台刷新。
- 环境覆盖不构成恶意 agent 的安全隔离；显式清除环境、指定其他 `--author` 或保留历史作者的操作不在强制防篡改范围。

### 实施顺序与验收

1. 补齐 bot 查询和身份字段，更新 wire 版本、相关 fixture 和恢复合同。
2. 接入共享任务环境与 jj 未发布工作提交准备，保持首轮/修订/answer 的原交付边界。
3. 更新当前部署文档，在现有测试文件中保留最少的行为回归。
4. 使用本地临时仓库和 bare remote 验证，不操作真实 GitHub 写接口；通过相关 pytest、Ruff 和类型检查后交付本地改动。需要发布时遵守同一 PR 和代码审查 gate。

拟执行的验证：

- 实际 Git 提交并读取 Author/Committer，冲突输入包括不同的全局、仓库、Author 环境和 Committer 环境身份；执行后全局与仓库配置内容不变。
- 实际 jj 普通提交及已有个人 `@` 的恢复场景，验证作者和提交者均为 bot、文件内容不变、已发布祖先 SHA 不变；只读 answer 的提交 SHA 不变。
- Codex/Amp start/resume 都使用同一 bot 身份，session fallback 不丢失；无编码上下文的负对照不被改成 bot。
- bot lookup 成功、404、普通用户、login 不符、无效 ID；身份字段跨 capability/wire/worker 传递正确，旧协议拒绝，无 token 泄漏。
- 消融一：去掉 Committer 覆盖，冲突 Committer 用例必须失败。消融二：保留环境注入但移除 jj 当前工作提交处理，已有个人 `@` 的作者用例必须失败。消融在实现后、代码审查前执行。

### 尚待实施时验证的细节

- jj 受支持版本中等价元数据命令、已发布历史识别与工作提交准备的具体落点；以临时仓库和现有 revision 测试决定，不放宽原交付校验。
- assignment 恢复是否保存 wire secret，以及版本变化对存量任务的影响；由实现前读取 journal 路径确认。
- GitHub 界面的最终归属需要未来获准发布真实任务后验证；本地测试不声称已验证 GitHub 页面。

方案由本机 Claude Code 的 `claude-opus-5-5` 辅助草拟，主线程已核对上述代码、官方资料及 jj 作者保留行为。初始化和结果均报告该模型，工具与 MCP 列表为空；模型草稿不作为执行验证。

## 2. PR change timeline

### [001] — 2026-11-13 — 收窄为 workspace Git 配置与原账号合著署名

- **用户要求：** “看一下 gitbot 的 skill 怎么做的，我们只在 workspace 里的 git 配置注入 bot 账号，然后把旧配置的账号设置成 co=commiter”。本次是修改方案方向，尚未实施产品或测试改动。
- **当前理解：** bot 成为 workspace 生效的 Git `user.name/user.email`；写入前读取原本生效的账号，将其保留为 GitHub 可识别的 `Co-authored-by: 原名称 <原邮箱>`。`co=commiter` 暂按合著者理解，不新增非标准的 `Co-committer` 字段。
- **替代范围：** 不再采用初始方案中每轮强制注入四个 Git 身份环境变量、覆盖 jj 环境和自动改写 jj 工作提交作者的做法。不修改个人全局配置或已发布历史。bot 身份的具体来源及是否需要改变任务载荷，待对照用户指定 skill 后确定，不沿用初始方案中的协议升级结论。
- **已查看的参考：** 本机 `/Users/b1indsight/.local/bin/gh-bot` 只生成 installation token 并在子进程中调用 gh；`following-tool-rules` 只要求通过此包装使用 GitHub CLI，均没有设置 Git 作者、committer 或 coauthor。已检索本机常用 skill 目录、用户 skills 仓库及共享 skill 发现接口，未找到名为 `gitbot` 或 `git-bot` 的 skill，需要用户提供路径或链接。
- **工作区隔离边界：** 当前 worker 同时支持完整副本和共享 seed 的 linked worktree，见 [`RepositoryWorkspaces`](../../app/worker/execution/workspaces.py)。在 linked worktree 中直接执行 `git config --local user.*` 会改共享 Git 配置，不能当作 workspace 隔离；实施时须采用 worktree 专属配置，并验证 seed 和兄弟 worktree 不受影响。
- **合著者落地边界：** Git 配置中保存原账号本身不会自动为每条提交添加 trailer；仅有 `commit.template` 也不能覆盖 agent 使用 `git commit -m` 的路径。须先核对参考 skill 如何在实际提交中追加标准 `Co-authored-by`，避免只保存一个无人使用的配置值，也不在证据不足时自行引入 hook 方案。
- **探查证据：** 已用一次性 Git 仓库与两个 linked worktree 执行 `config --local`、启用 `extensions.worktreeConfig` 后的 `config --worktree`、以及带 `commit.template` 的 `commit --allow-empty -m`。观察到前者改变 seed/兄弟身份，worktree 配置只改变当前身份，显式 `-m` 提交没有追加模板中的 coauthor。临时仓库已清理，未操作实际 worker 配置。
- **待确认：** 用户所指的 `gitbot` skill 的位置；旧账号的目标语义是否确为 `Co-authored-by`。原账号捕获需能重复运行而不把已注入的 bot 当成原账号，且不能复用其他 Flow 的旧身份。
- **后续验收方向：** 真实 Git 提交显示 bot Author/Committer 及原账号 coauthor；原全局配置、seed 与兄弟 worktree 不变；重复初始化不覆盖保存的原账号、不重复追加 trailer。此处仅记录拟议验收，尚未运行实现测试。

### [002] — 2026-11-13 — workspace 配置与提交消息 hook

- **要求澄清：** 用户确认“就是 gh-bot，旧账号用 Co-authored-by”。所指参考已定位；`gh-bot` 的 installation token 认证不能决定 Git 的提交作者，故不复制其认证实现或私钥配置。
- **身份来源：** 沿用 control 的 `bot_login`，在 `Repository.create_push_capability` 中通过 installation 客户端只读查询 bot，核对 login、Bot 类型及正数用户 ID。现有 capability/secret 携带两个必填字段，wire 升至 23；保持 token 权限、journal5、inbox 与数据库格式不变。恢复不保存 secret，重新派发领取新能力。此处是实际选择，不再保留 [001] 的来源未定状态。
- **配置落点：** 新增 `git_identity.py` 负责 workspace Git 身份和消息 hook，`RepositoryWorkspaces` 在既有仓库锁和所有权校验内调用。首轮完成原仓库校验后、修订完成冻结基线校验后准备配置，answer 跳过。原有效作者优先取 `author.*`，否则 `user.*`；只捕获一次，缺失时不虚构合著者。bot 写入 `user.*`、`author.*`、`committer.*`，不注入身份环境变量。移除环境继承中四个 Git 身份变量的例外，避免覆盖 workspace 设置。
- **共享边界：** 身份、原合著者及 hooksPath 均写入 `config.worktree`。linked worktree 必须在 shared config 启用 `extensions.worktreeConfig`，必要时将 `core.worktree/core.bare=true` 移到 main worktree 配置；因此 [001] 的“seed 不变”准确范围为 seed 和兄弟工作区的身份与 hooks 行为不变，不能声称共享配置逐字节不变。个人全局配置不写入；共享仓库锁串行化扩展启用和配置迁移。
- **消息处理选择：** 使用 `commit-msg` hook，在已有同名 hook 成功后调用 `git interpret-trailers --if-exists addIfDifferent`，保留其他 coauthor 并防止重复。其他已有可执行 hooks 通过包装调用原路径，保留参数、退出状态和脚本自身路径。选择 `commit-msg` 是因为实际探查与先失败的回归确认：`prepare-commit-msg` 发生在编辑器之前，会漏掉随后编辑的信息。空消息不追加 coauthor，保留 Git 的取消语义。显式 `--no-verify` 按 Git 原生语义跳过此 hook。
- **替代与限制：** `commit.template` 不能覆盖 `-m`；`--local` 不能隔离 linked worktree；只设 `user.*` 不能覆盖旧 `committer.*`。不采用这些较简方案。没有原账号时不构造个人身份；已是 bot 时不追加自身。纯 jj workspace 不执行 Git 配置，colocated 仓库不改 jj 设置或元数据；jj/commit-tree 不运行 Git hook，amend 保留原作者的语义不变。不追加针对恶意 agent 的防篡改机制。
- **实现验证：** 新回归先因缺少配置接口失败，补上实现后验证真实提交；交互编辑场景先观察到 coauthor 缺失，再改为 `commit-msg` 后通过。相关测试、完整 PostgreSQL 测试及 Ruff、Mypy 已通过；消融结果见 [003]。所有提交/推送验证均使用临时仓库和本地 bare remote，没有实际 GitHub 写操作。
- **设计辅助：** 本机 Claude Code 实际初始化及结果模型均为 `claude-opus-5-5`，工具与 MCP 列表为空。采纳其关于 `author/committer.*`、hook 时机、原 hook 路径和空消息的检查项；未采用其环境配置建议，因为用户限定 workspace 配置。模型建议与实测结果分别记录，不将建议当作验证。

### [003] — 2026-11-13 — 实际提交、隔离与较简方案消融

- **验收结果：** 在本机 Git 2.50.1 中验证完整副本及 linked worktree 的真实提交；bot Author/Committer、原账号 coauthor、已有 Amp coauthor、重复准备、显式 `-m` 及编辑器提交都符合预期。原 hooks 继续执行且能拒绝提交，个人全局配置逐字节不变，seed/兄弟身份不变。现有首轮本地 clone/push/MCP 和修订交付测试验证配置接入，answer 测试确认不创建或修改 Git 配置。
- **命令与环境：** 相关测试使用 `uv run pytest -q tests/test_worker_workspaces.py tests/test_worker_executor.py tests/test_revision_delivery.py tests/test_worker_github_environment.py tests/test_amp_driver.py tests/test_codex_driver.py tests/test_github_client.py tests/test_protocol.py tests/test_worker_session.py`；全量执行 `uv run pytest -q`，使用专属临时 PostgreSQL 17 的 loopback TCP URL 作为 `FLOWTOOL_TEST_DATABASE_URL`。`uv run ruff check app tests`、`uv run mypy app tests` 和 `git diff --check` 通过，恢复 smoke fixture 的新增身份字段已同步并通过编译检查。精确运行结果保存于线程验证记录，不将易变测试数量固化在本文。
- **测试环境修正：** 首轮完整运行使用 Unix socket，恢复连接测试在校验 loopback IP 时失败；核对 `scripts/smoke/recovery.py:freeze_connection` 后改用临时数据库的 TCP 端点，重新完整运行通过，没有修改该恢复逻辑。临时数据库启动时使用 `LC_ALL=C`，未修改既有数据库服务或账号配置。
- **消融一：** 在完成同样配置的临时仓库执行 `git config --worktree core.hooksPath /dev/null`，再 `git commit --allow-empty -m Scoped`；作者/提交者仍是 bot，但 coauthor 缺失。保留消息 hook，配置本身不足以满足要求。
- **消融二：** 删除 worktree 层的 `committer.name` 和 `committer.email`，保留 bot 的 `user.*`；旧的显式 committer 配置重新生效，生成个人 Committer。保留 `author.*`/`committer.*` 的专属覆盖。
- **消融三：** linked worktree 改用 `git config --local user.name`，seed 随即读到相同 bot 名称。保留 `--worktree` 及必要的共享扩展开关；另用既有 `core.worktree` 验证迁移后 main/linked 路径与 seed 身份正确。
- **边界探查：** 全局原账号包含引号和中文时 trailer 正确；原账号缺失或原本就是同一 bot 时不追加合著者；空编辑器退出时 Git 拒绝空消息且 HEAD 不变。相对 hooksPath 能定位到 workspace 内的绝对路径；纯 jj workspace 跳过 Git 准备。探查仅使用自动清理的临时仓库，不保留一次性测试脚本。
- **交付状态：** 产品代码、fixture、部署说明和本记录均为本地改动，未提交、推送、创建 PR 或部署。运行中的实际 worker 尚未切换；未来发布需协调 control/worker 升级到 wire23。

### [004] — 2026-11-13 — 初始化一次、提交时检查与当前 App 身份绑定

- **用户要求：** “初始化workspace时设置一次，后面添加Co-authored-by 时检查；bot 账号要和当前app是属于同一个账号”。本次已实现，替代 [002] 的每轮重写配置及仅验证配置 bot login 的行为；父目录配置不作为自动继承机制。
- **配置生命周期：** `configure_git_identity` 在初始化完成后最后写入 worktree 专属的 `flowtool.botIdentity`。后续首轮重试、修订及恢复只比较当前 capability 的 bot 身份，不再写配置或 hooks；login 或用户 ID 不符时失败，不替换旧账号。保留现有调用落点，允许升级后的既有 workspace 在首次编码准备时初始化，answer 继续跳过。先确认 `extensions.worktreeConfig` 已启用再读取专属标记，因为尚未启用扩展的多 worktree 仓库不接受 `git config --worktree`。
- **提交检查：** `commit-msg` 在运行既有 hook 后、追加 coauthor 前，用 `git var GIT_AUTHOR_IDENT/GIT_COMMITTER_IDENT` 读取实际身份，去除时间戳后按完整姓名和邮箱做字面比较；不匹配则拒绝提交，不自动修复配置。初次捕获的原账号和其他 coauthor 继续保留并去重。该机制检查正常 hook 路径，不阻止 `--no-verify` 或移除 hook 的主动绕过。
- **App 绑定：** control 的 `Repository.create_push_capability` 使用当前 App JWT 客户端调用 `GET /app`，核对数值 App ID 和非空 slug，并要求配置的 bot login 与 `slug[bot]` 忽略大小写一致，然后查询该 bot 的真实用户 ID 并保留原有 Bot 类型校验。其他 App 的 Bot 不能作为当前 capability 身份；不增加 worker 网络访问、身份环境变量、协议字段或缓存。
- **行为边界：** Git 的 `--amend` 若保留个人作者会被拒绝，显式 `--reset-author` 使用当前 bot 时可通过署名检查，但既有祖先和交付基线核验仍生效。原有 INITIAL 修复回归的模拟 agent 改为显式 reset-author，继续构造并校验原本的祖先违规，没有放宽生产逻辑。README、部署说明和 changelog 已同步。
- **回归与验证：** 扩展既有真实 Git workspace 回归及 capability 回归；旧实现先出现身份被再次覆盖和错误 App 被接受的失败，新实现通过。完整 PostgreSQL 测试、Ruff、Mypy 及 diff 空白检查通过，日志保存在本线程 `.amp/in/artifacts/worker-identity-amendment-{red,full,checks}.log`。GitHub API 使用测试替身，没有对实际 App 发请求，不声称已核对部署中的账号。
- **实际对照：** 一次性真实 Git 仓库中，重复准备前后的配置、hook 内容及 mtime 不变；显式 author、committer 环境变量、命令级配置和保留个人作者的 amend 被拒绝，HEAD 不变；bot amend 及显式 reset-author 保留唯一 coauthor，空消息取消语义保留。移除初始化标记后，准备会再次覆盖身份偏移；移除提交身份检查后，错误 committer 带 coauthor 被成功提交。因此保留这两处机制。证据为 `.amp/in/artifacts/worker-identity-amendment-probes.log`，临时仓库和本轮专属 PostgreSQL 已清理。
- **设计辅助与交付：** 本机 Claude Code 初始化及响应模型均校验为 `claude-opus-5-5`，工具与 MCP 列表为空。采纳完整身份字面比较和最后写入标记；不加入未来版本标记及复制已初始化 workspace 的兼容层，当前创建路径只从未注入身份的 seed 复制。所有改动仍在本地，未提交、推送、创建 PR 或部署。

### [005] — 2026-11-13 — 从 App 自动解析 Bot，移除手工配置

- **触发与用户要求：** 用户报告 Job 24 在下发前因 `assignment capability failed: GitHubError` 失败，并提供相同 App ID 下配置 `b1indsight[bot]`、实际 `b1indsight-bot[bot]` 的只读查询结果，随后要求“直接改成从 App 获取，去掉手工 Bot 名配置”。本次已实现，替代 [004] 的手工 login 比较和每次签发重新查询 App 的选择，不改变 workspace 初始化和提交检查。
- **单一来源：** `GitHubApp.resolve_bot_login()` 使用 App JWT 的 `GET /app` 核对数值 App ID 与非空 slug，派生 `slug[bot]`；`ServerSettings.bot_login` 与部署示例中的环境变量移除。旧环境变量由现有 `extra="ignore"` 策略忽略，不作为回退。`Repository.create_push_capability` 使用该身份，保留 installation 下的真实 Bot 用户、正数用户 ID、返回 login 和 token scope 校验；wire、worker 和持久格式不变。
- **所有权与生命周期：** 身份保存在共享 App 对象的私有成功缓存，异步锁合并并发首次查询，失败不缓存。同步属性只读已解析身份，未解析即报错，不隐式联网或返回默认名称。进程存活期间复用同一身份；App slug 变化后须重启 control 重新读取，不增加配置或后台刷新。
- **消费者边界：** 普通评论与线程 marker 恢复可在没有任何 capability 的新进程自行解析身份，保留 Bot 类型与整行 marker 限定。Webhook 在领取 receipt 前解析，之后只把已解析字符串传给 DB-only router；失败时 receipt 状态和尝试次数不变，下轮重试。反馈轮询在资格事务结束后解析，评论和线程过滤使用同一身份，均忽略大小写；合法关联合并事实仍绕过作者过滤。Runtime 的同步 startup/readiness 和取消时等待 DB 线程退出的合同不变。
- **测试与验证：** 扩展既有 capability、评论/线程恢复、router 与反馈测试，新增身份失败后重试/并发共享、Webhook 无身份不领取与恢复的最小回归。先运行旧实现并观察带 `-bot` slug 的 capability 与评论恢复失败，再验证修改后的关联测试通过。归档凭据失效测试显式使用已解析的 App 身份，继续隔离 installation-token 的原失败边界；冷启动身份解析由独立恢复回归覆盖。使用本轮专属 loopback TCP PostgreSQL 17 执行 `.venv/bin/pytest -q`，首轮仅该旧归档夹具失败；适配后执行 `.venv/bin/pytest -q tests/test_archive_status.py tests/test_router.py tests/test_reconcile_feedback.py` 及 `.venv/bin/pytest -q --lf` 复测通过。其余全量用例通过或按既有条件跳过。Ruff、Mypy 与 diff 空白检查通过，未放宽产品身份或回执校验。证据分别见本线程 `.amp/in/artifacts/app-bot-identity-{red,integration,full,archive-recovery,final-rerun,checks}.log`；专属测试数据库已停止并清理，没有操作既有数据库或生产服务。
- **较简方案对照：** 使用相同身份回归，在独立 Python 进程将锁替换为无操作上下文，失败一次后并发的三个调用产生三个成功请求，总请求由两个变成四个；仅移除缓存时，后续读取再请求一次，总数变成三个。两项都触发预期测试失败，源文件未改动。因此保留成功缓存与单次查询锁；不引入额外身份模型、匹配 helper 或全局启动网络 gate。可复现方法为 `pytest.main(['-q', 'tests/test_github_client.py', '-k', 'app_bot_identity'])` 配合上述单一替换，证据见本线程 `.amp/in/artifacts/app-bot-identity-ablation.log`。
- **设计辅助与交付：** 本机 Claude Code 初始化及响应均确认 `claude-opus-5-5`，工具与 MCP 列表为空。采纳 App 自有异步解析、同步只读身份和 router 接收字符串的边界，未增加匹配 helper。当前仅本地修改，未提交、推送、部署或重试生产任务；Job 24 是否恢复须在另行升级并重试后验证。
