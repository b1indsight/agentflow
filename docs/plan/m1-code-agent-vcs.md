# M1 CodeAgent 本地 VCS 自治计划

> 状态：已实施并验证 · 计划日期：2026-09-02 · 实施日期：2026-09-03 · M1 Feature 1/3
>
> 本 feature 只证明 CodeAgent 在隔离工作区内自主完成改码、验证、commit 与 push。GitHub App
> 认证和线上 PR 操作属于 Feature 2；真实 Issue 到 PR、Flow tool RPC 与 Flow-owned delivery 属于
> Feature 3。本计划采用首版简化约束：一个 Flow 独占一个 worker execution slot 和完整 repo 目录直到
> Flow 终结；checkpoint、跨 worker 迁移与重建以后再加。

## 1. 背景与目标

当前 `WorkerExecutor` 创建工作区后把 VCS 指引放进 prompt，`CodexAgent` 运行一次非交互 `codex exec`。
目标语义已经要求 CodeAgent 自己建立 branch/bookmark、commit 和 push，但当前实现只依据进程 exit code
返回成功，没有证明本地提交或远端分支真实存在。

本 feature 固定以下 ownership：

1. Flow 是 repo、base、目标 branch 和交付身份的持久 owner；FlowTool 把这些约束投影给本次 attempt；
2. CodeAgent 自己读取 VCS 状态，决定命令顺序、纳入提交的文件和 commit message，并执行 commit/push；
3. worker 不在 CodeAgent 退出后代替它 staging、commit 或 push，只读取结果做验证；
4. GitHub PR、评论、label 和 reviewer 永远由 control plane 执行；CodeAgent 不直连 GitHub API，后续只
   能通过受约束的 Flow 环境工具提出请求；
5. 一次成功运行必须同时产生 CodeAgent session、本地目标 commit 和可验证的远端 branch。

完成后，使用本地 fixture remote 的真实 Codex smoke 能从一个干净 base 生成并推送一条目标分支；失败
路径不会把“Codex exit 0”误报成 VCS 成功。

## 2. 范围与非目标

### 2.1 本次包含

- 为 CodeAgent 定义 repo/base/branch/remote 的显式执行上下文，由 Agent 检查仓库后自行选择 Git 或 jj；
- 保持自然的 Git/jj 命令交互，由 CodeAgent 主动执行 commit 和 push；
- CodeAgent 在 Flow 独占 repo 中不使用 `workspace-write`，以 `danger-full-access` 直接操作完整 `.git/.jj`；
- 同一 VM 可以运行多个使用不同独占目录的 worker；目录隔离表达资源所有权，CodeAgent 与 control
  secret 之间另由专用 OS account、container 或 VM 建立安全边界；
- 每 Flow 一个独占 repo 目录；同一 Flow 的 Attempt 串行复用，不同 Flow 永不共享 working tree 或
  可写 VCS metadata；
- CodeAgent 结束后的只读结果检查：branch、commit SHA、工作区状态和远端 ref；
- 区分 agent、测试、commit、push、结果解析等失败原因；
- 确定性单元/集成测试，以及一次记录在计划实施结果中的真实 Codex smoke。

### 2.2 本次不包含

- GitHub App JWT、installation token、GitHub REST/GraphQL 或真实 GitHub PR；
- 从 Webhook 获取真实 Issue 内容，或修改 INITIAL 远程协议；
- review/triage/resume、合并、archive、reconcile、冲突恢复或取消运行中 Codex；
- 自动创建独立 OS account、容器/微 VM 或通用多租户安全边界；M1 仅支持同一信任域；
- worker 自动发现或 clone 任意仓库；目标 repo 仍需预置在 worker root；
- Flow checkpoint、跨 worker 迁移或在新机器重建 repo/session；worker 丢失时不迁移；
- worker 在 agent 退出后自动修补未提交改动或替 agent 选择 commit message；
- Flow-owned delivery 持久化、Flow tool RPC、`flow.open_pr/spawn/wait/accept`、Flow 派生或父级验收；
  这些由 Feature 3 及后续计划承接。

## 3. Ownership 与执行契约

### 3.1 FlowTool 提供约束

worker 为每个 Flow execution 构造不可歧义的执行上下文：

- `repo_path`：当前 Flow 独占的完整 repo 目录，包含自己的 `.git/.jj`；
- `base` / `branch`：为 Flow 选定并投影到本次 attempt 的基线和目标分支；
- `remote`：唯一允许的发布目标；
- `execution_policy`：确认当前 worker/slot 只绑定此 Flow，并由部署方显式选择 trusted-host 模式，允许 Codex
  直接操作 repo；
- `credential`：本 feature 只使用无密钥的本地 remote；真实短时凭据由 Feature 2/3 接入。

这些字段是能力和约束，不是预先编排好的 VCS 命令列表。`context_builder` 告诉 CodeAgent 完成条件，
但不替它决定 commit 边界、message 或具体命令序列。

本 feature 不新增 delivery 或完整 Flow lease 表；现有 Attempt 字段可以暂存 branch/worker，但 repo
目录和 branch 的逻辑 owner 是 Flow。实现和测试不得依赖“不同 Attempt 拥有不同 repo/branch/PR”的语义。
Feature 3 再持久化 Flow-owned delivery 与生命周期 lease，并保持本 feature 的 VCS 接口不变。

### 3.2 CodeAgent 保持决策权

CodeAgent 必须在同一 session 内：

1. 阅读仓库约定并检查实际 metadata/状态，自主选择 Git 或 jj，再基于指定 base 建立目标
   branch/bookmark；
2. 修改代码并运行仓库约定的测试/检查；
3. 检查 diff，选择应提交内容并生成 commit message；
4. commit 并 push 指定 branch；
5. 返回简洁总结。

CodeAgent 在结果中声明 `vcs_backend_used`；worker 只在它返回后按声明路径读取实际 VCS 状态，形成
`verified_backend + branch + commit_sha + remote_sha` 的结构化事实。Agent 声明不是可信输入，声明与
metadata、目标 branch 或远端事实不一致都使 INITIAL 结果失败。

## 4. Flow 独占执行环境与 VCS

### 4.1 首版生命周期所有权

首版不做 checkpoint/restore。一个 Flow 首次 Started 时取得一个 worker execution slot 和该 slot 下的
独占 repo 目录，绑定持续到 Flow `succeeded / failed / cancelled / archived`：

- 当前 worker 是单槽；绑定期间即使没有 running job，也不能领取其他 Flow；
- 同一 Flow 的 initial、未来 revision/answer/archive job 回到同一 worker 和 repo；
- 同一 Flow 的 Attempt 只能串行进入该 repo；
- 不同 Flow 必须使用不同 repo 目录，不共享 `.git`、`.jj`、index、refs 或 working tree；
- worker 短暂断线只允许同 identity 恢复原目录；超过 recovery grace 后 Flow 明确失败，不调度到另一台机器；
- terminal cleanup 完成后才释放 slot；M1 尚无 archive 时保留目录与绑定，供后续 feature 接管。

可以共享只读下载缓存或 seed mirror，但不能把它们作为任一 Flow 的可写 VCS metadata。checkpoint、跨机器
重建和旧 lease fencing 属于后续扩展。

### 4.2 Codex 与 VCS 自探测

Codex 在 Flow 独占 repo 中以 `danger-full-access` 运行，不使用 `workspace-write`，因此可以直接修改 repo
自己的 `.git/.jj`。同一 VM 可以承载多个 worker，每个 worker 使用不同目录并同时只绑定一个 Flow。

这里的“独占”是 scheduler 和 repo 所有权约束，不是 OS 安全边界：同一 account 下的
`danger-full-access` 进程原则上可以访问兄弟目录。因此 worker 必须在无法读取 control-plane credential
或域外敏感数据的专用 OS account、container 或 VM 中运行；不同目录本身不能满足此要求。互不信任租户
仍需要每 Flow 容器或微 VM。

VCS 选择发生在 CodeAgent 内，不由 control 或 worker 注入。prompt 只要求检查仓库约定和实际 metadata；
Git-only、jj-only 和 colocated 仓库都由 Agent 自主选择并执行原生命令。worker 在 run 结束后依据 Agent
声明的 backend 做只读验证，不增加 VCS broker，也不在 Agent 退出后自动 commit/push。

### 4.3 网络与凭据接缝

本 feature 的自动化和真实 Codex smoke 使用 Flow 独占 repo 与本地 bare remote，不要求 GitHub token。
由于 Codex 自身 sandbox 已关闭，后续网络限制由外部执行环境负责：

- 默认不提供公网出口；Feature 2/3 需要 push 时只放行允许的 GitHub 目标；
- credential 通过运行时 credential helper 提供，不写入 prompt、repo、结果或 checkpoint；
- worker/control credential 不进入 Codex 子进程环境；worker 不自动读取 control 使用的 `.env`，full-access
  模式还会拒绝继承 control-plane 环境变量；
- 日志只记录 credential 是否存在和过期时间，不记录值。

能够执行 push 的代码原则上有机会读取 push credential；M1 通过独占 repo、短 TTL、网络目的地限制和
不持久化来限制风险，这些措施不构成多租户安全隔离，也不能声称 secret 对 CodeAgent 绝对不可见。

## 5. 模块与文件布局

- `app/vcs.py`
  - 准备并验证 Flow 独占的完整 repo root；同一 Flow 复用、不同 Flow 拒绝复用；
  - Git/jj metadata 的机械发现、通用自探测 guidance 和按实际 backend 的只读结果检查；
  - 不新增 run 结束后的自动 commit/push。
- `app/models.py`
  - 增加内部 VCS execution context/outcome 类型，记录 Agent 声明与 worker 验证的 backend，并区分
    local/remote SHA 与失败阶段。
- `app/ports.py`
  - 扩展 CodeAgent start 输入，使 adapter 接收显式执行环境与 execution policy，而不是读取全局状态。
- `app/codex_driver.py`
  - 只有部署配置显式启用 trusted-host execution policy 时才使用 `danger-full-access`；
  - 构造最小子进程环境并剥离 worker/control credential；
  - 保持 prompt、命令和日志脱敏。
- `app/context_builder.py`
  - 把 base、branch、remote 和成功条件写清楚，禁止 CodeAgent 直调 GitHub API；Feature 3 才通过
    `flow.open_pr` 请求 control plane 开 PR。
- `app/worker/executor.py`
  - 校验 job 所属 Flow 与当前 slot/repo binding 一致，再组装上下文、运行 agent、只读验证 outcome。
- `tests/`
  - 增加 VCS fixture、Codex 参数契约和 worker INITIAL 行为测试。

具体类型名可在实现时按现有 Pydantic/dataclass 约定调整，但不能改变上述 ownership。

## 6. 实施顺序

1. **Flow 独占 repo 契约，测试先行**
   - 建立 Flow repo + 本地 bare remote fixture；
   - 证明同一 Flow 的串行 Attempt 复用同一路径，不同 Flow 永不取得同一 repo；
   - Flow 终结前不释放 slot/repo；worker 丢失超过 recovery grace 后失败且不迁移。
2. **执行上下文与结果模型，测试先行**
   - 固定 repo/base/branch/remote 输入和 backend-aware outcome/失败阶段，不注入 `vcs_kind`；
   - 覆盖 Git-only、jj-only、colocated、声明/backend 不一致、错误 branch/SHA、没有 commit、没有 push、
     dirty workspace。
3. **Codex adapter 与 prompt，测试先行**
   - 只在显式 trusted-host 配置下启用 `danger-full-access`，校准最小环境、网络前置条件和日志脱敏；
   - 断言 prompt 赋予任务和约束，不硬编码 commit message 或替 agent 调 VCS。
4. **Worker INITIAL 接线**
   - 校验 Flow/slot/repo binding，组装上下文，运行 CodeAgent，读取并验证 VCS outcome；
   - 保持现有远程调度、journal、lease 和 result replay 行为。
5. **Git/jj 集成与文档**
   - Git 跑确定性 local-remote 集成测试；jj 跑等价回归；
   - 记录真实 Codex smoke 的命令、环境、结果和已知边界；
   - 同步架构文档中“VCS 只靠 prompt 且未验证”的状态。

## 7. 测试与验收

自动化测试：

- 每 Flow 获得独占 repo root；同一 Flow 的串行 Attempt 复用它，不同 Flow 的路径和可写 VCS metadata
  不相交；
- Flow 非终态时 slot/repo 不释放，其他 Flow 不能进入该 worker；worker 丢失时不迁移；
- guidance 要求 CodeAgent 自行检查 VCS，同时包含指定 base、Flow-owned branch、remote 和禁止直调
  GitHub API 的边界，不注入预选 `vcs_kind`；
- Git-only、jj-only 和 colocated fixture 都验证 Agent 选择与 worker 独立核验的一致性；
- CodeAgent exit 0 但无 commit、未 push、错误 branch 或 SHA 不一致时失败；
- CodeAgent 自己产生的 commit message 和提交边界被保留，worker 不做后处理提交；
- `danger-full-access` 只允许在配置明确选择 trusted-host 模式时启动；子进程环境不包含 worker/control
  credential；
- 现有 worker affinity、journal、recovery 和协议测试不回退。

真实 smoke：

- 使用一次性 fixture repo 与 bare remote；
- 给 Codex 一个可确定验证的小任务；
- Codex 自己检查状态、改文件、跑测试、commit 和 push；
- 远端目标 branch 的 commit 与 worker 检查结果一致；
- CodeAgent 只触碰该 Flow 独占的完整 repo，不触碰 seed mirror 或其他 Flow repo；
- 记录 Codex CLI 版本、Agent 实际选择及 worker 验证的 VCS backend、commit SHA 和验证结果，不记录
  账户凭据或完整会话内容。

常规验证命令：

```bash
uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv lock --check
uv build --no-sources
git diff --check
```

## 8. 风险与回退

- **目录不是安全边界**：M1 允许多个 worker 在同一 VM 的不同目录运行，但启用 `danger-full-access` 的
  worker 必须位于无法读取 control-plane secret 的专用 OS account、container 或 VM。不同目录只提供
  Flow 所有权隔离，不能替代 secret 或多租户隔离。
- **容量利用率**：首版在 Flow 等待期间也保留单槽 worker，吞吐量较低；这是避免 checkpoint/迁移复杂度的
  明确取舍，后续再引入可恢复调度。
- **worker 丢失**：首版不在另一台机器重建 repo/session；recovery grace 结束后明确失败。
- **prompt 不稳定**：不能只相信最终文本；worker 必须读取本地与远端 VCS 事实。
- **agent 可使用 push 权限**：Feature 2/3 必须收窄 repo、TTL 和网络目的地；分支保护仍由目标仓库负责。
- **Git/jj 行为漂移**：共享 outcome 语义、分开 fixture，避免为了统一接口隐藏真实差异。
- **repo 残留**：失败时保留 Flow repo 供诊断；terminal cleanup 和保留期限由 archive/cancel 后续语义负责。

如果部署环境不能提供 Flow 独占完整 repo、单槽生命周期绑定，或无法避免向 CodeAgent 暴露域外敏感数据，
本 feature 停在失败的 feasibility gate，回到同一计划 PR 调整执行环境；不得悄悄把 commit/push 转给
worker。

## 9. 实施结果（2026-09-03）

- `prepare_flow_repo` 以稳定 `flow_id` 派生完整 repo：同一 Flow 串行复用，同 branch 的替代 Flow 也不复用；
- INITIAL 使用严格结构化 schema 接收 Agent 自报的 `vcs_backend_used`，worker 再独立验证 Git/jj metadata、
  目标 branch/bookmark、新且基于原 checkout 的 commit、干净状态、未变化的预捕获 remote 和远端 SHA；
  验证命令使用净化环境、禁用 Git hooks/helper/external protocol，并带进程组级 timeout；exit 0 不再单独代表
  VCS 成功；
- INITIAL VCS 失败通过结构化 stage/detail 回传 control plane 并进入失败消息，不再只留下 exit 65；
- worker wire protocol 升到 v3，让 repo-bearing task 显式携带 stable `flow_id`；
- Codex start 改为显式 trusted-host `danger-full-access`，并以最小环境启动，不向子进程继承
  `FLOWTOOL_*`、`GH_TOKEN` 或 `GITHUB_TOKEN`；
- 自动化覆盖 Git-only、colocated jj、backend mismatch、无新 commit、repo 复用/分离和启动参数；
- 真实 smoke 使用 `codex-cli 0.152.1`、一次性 Git repo 和本地 bare remote，Codex 自主生成 commit
  `2972371a533dc069754edf31e33886229b7a4e77` 并推送 `flow/codex-smoke`，本地与远端 SHA 一致；
- Flow 生命周期 worker binding、GitHub credential/egress 和跨协议 VCS outcome 仍按计划留给 Feature 3。
