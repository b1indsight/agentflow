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

## 10. 2026-11-12：INITIAL 校验失败反馈给原 CodeAgent（待批准）

**提议：** 首次编码完成后的可修正校验错误先反馈给原 CodeAgent，让它在同一任务内继续修正；完整校验通过后才交付。保留原始基线、独立验证和审查门禁，不让 worker 代替 Agent 修改仓库。本节仅为计划，尚未修改产品代码、测试或正在运行的 Flow。

### 动机与范围

用户要求：“这些问题你应该回报给code agent而不是直接显示失败”。触发案例是 Flow 3 的 INITIAL job 17：Agent 把运行前已有的未推送实现提交 amend 为同父提交，随后 Worker 的祖先校验直接返回 exit 65；Agent 还报告审查超时，未完成 push 和 `flow.open_pr`。[原执行记录](https://ampcode.com/threads/T-01a1ce3b-bfb0-7576-8137-3da6df2b8df8)与[本次反馈](https://ampcode.com/threads/T-01a1ce56-6fbd-7507-bf93-85c83f847b1a)是需求依据。

当前 [`WorkerExecutor._initial`](../../app/worker/execution/executor.py) 在 Agent 返回后关闭 MCP，再解析输出、验证 VCS 和交付回执，失败即结束任务。两个 [`CodingAgent`](../../app/domain/ports.py) adapter 已提供带 cwd、schema 和当前能力的 `resume`，可以复用，不需要新执行后端。

本批只修改上述 INITIAL 完成路径，覆盖结构化输出、分支/提交/工作区/推送状态及缺少交付回执。REVISION 的持久 fallback 证据、controller 远端结果校验、凭据续期、审查工具内部重试和通用任务重试不在本批范围内。不解析自然语言 summary 来判断审查是否通过，也不自动绕过超时的审查。

### 拟采用行为与边界

1. 初次 `start` 结束并成功返回后，仍在同一个 `WorkerMCP` 生命周期内执行全部完成检查。校验未通过时保持 job 为执行中，不先发终态、创建新 attempt 或要求用户重试。
2. 将可修正的错误阶段、安全详情、固定目标分支和原始 Git/jj 基线反馈到这次成功运行的准确 session，调用 `CodingAgent.resume`。继续使用同一 assignment、workspace、schema、MCP 和当前 capability，最多追加两轮纠正；不新增配置项。
3. 原始 `VcsExecutionContext` 只捕获一次。每轮都重新解析严格输出，独立验证 backend、祖先关系、工作区、原 remote 和远端 SHA，以及交付回执。不能把 amend 后的 HEAD 或基线的父提交改成新的验收基线。
4. 反馈要求 Agent 保留运行前基线为祖先、继续完成仓库审查及 push/交付，并返回原 schema。worker 不 staging、commit、reset、push，不替 Agent 处理阅读状态功能或审查发现。
5. 尚无成功回执时，Agent 修正后按原合同调用 `flow.open_pr`。已有成功回执时，保留已确认的 head 和回执，不重新执行交付调用；仅允许修正输出或把本地状态恢复到已验证的交付。远端已经偏离成功回执、需要改变已确认交付意图时停止，不盲目覆盖 controller 的 delivery。
6. 初次运行或纠正运行非零退出、超时、取消、没有 session、resume 拒绝或返回不同 session、assignment 失效、凭据过期、不可修正的工具执行错误均停止纠正。没有“执行过之后再开新会话”的 fallback；取消继续传播到现有进程组清理。
7. 纠正预算耗尽仍返回真实失败，保留最后阶段、详情及纠正次数。只有所有完成条件都通过才返回成功；不通过吞错或减少验证消除失败。

### 修改位置与接口

- `app/worker/execution/executor.py`：保持 `WorkerExecutor._initial(...) -> InitialTaskResult` 接口不变，将当前完成检查移入 MCP 上下文，加入固定两轮的同会话纠正循环。schema 文件在整个循环退出后才删除，不引入通用 retry 执行器、新表或新协议字段。
- `app/worker/execution/prompts.py`：新增 `build_initial_correction_prompt(context: VcsExecutionContext, stage: str, detail: str, delivery: DeliveryReceipt | None) -> str`。提示包含此次校验的安全诊断、原始基线、目标分支和 remote 名称、严格结果合同及是否已有交付回执；不回显原始 ValidationError、凭据、环境或任意 CLI stderr。
- `app/worker/execution/vcs.py`：为内部异常采用 `VcsVerificationError(stage: str, message: str, *, correctable: bool = False)`，仅对明确的完成合同不满足标记可纠正。进程超时、缺失可执行文件和命令失败保持不可纠正；Git 祖先查询的 exit 1 与命令错误区分，避免把所有 `commit` 阶段错误都当作可修正的历史问题。既有祖先和 remote 验证不放宽。
- `app/worker/transport/mcp.py`：成功回执存在后拒绝再次执行交付调用，保留原回执与 VCS outcome。未成功的调用仍沿用现有 RPC 关联及错误返回，不新增自动重放机制。
- `tests/test_worker_executor.py`、`tests/test_vcs.py` 与现有 MCP 测试：扩展公共执行结果和本地 remote 的回归，不按私有 helper 增加重复测试。

### 取舍、成本与恢复

只补 prompt 无法在错误发生后给 Agent 修正机会；放松祖先检查会接受错误历史；重新 `start` 会丢失原会话并可能重复副作用。同会话有界纠正保留原合同，代价是一次任务可能增加两轮模型调用及相应耗时。两轮是本方案的固定预算提议，不保证每个环境问题都能修复。

纠正属于仍存活的同一 execution task；现有心跳、取消和租约机制继续管理它。进程丢失后不根据缺失的内存循环自动恢复编码；现有 journal 的不确定执行拒绝重跑规则不变。无需数据库或 wire 格式迁移；上线需另行发布 worker，本计划不热补丁生产、不手动续跑 Flow 3、不改其 journal 或已有提交。

### 实施顺序与验证

1. 先用现有本地 bare remote 和受控 Agent 在公共 `execute` 接口复现：运行前历史为 P→B，初次运行 amend B 为 B′。旧实现直接失败；新实现把阶段和 B 反馈回同一 session，Agent 在 B 后追加 C，完成审查合同、push 与工具调用后返回成功。验证 remote、本地最终内容和 B 的祖先关系。
2. 冻结基线的负对照：纠正结果分别基于 B′ 和 P，仍必须失败；避免重新捕获 HEAD 或错误使用 B 的父提交。覆盖 Git 和已有 jj 祖先验证路径。
3. 覆盖非法结构化结果、未推送、dirty tree、缺少回执经纠正后成功，以及持续失败恰好耗尽预算。正常一次完成不增加模型调用；成功回执不能重复执行控制面交付。
4. 覆盖运行超时、无 session、session 变更或拒绝、过期能力、assignment 丢失和纠正中取消，不启动新会话、不报告假成功、不泄露测试凭据；结果重放不再启动 Agent。
5. 实施后运行 worker executor、VCS、MCP、adapter、journal 和 revision 相关回归，再运行全仓 pytest、Ruff、Mypy 与 diff 检查。不涉及 UI 外观，无需新增截图。这里列出的是待执行验证，不是已通过结果。

必要性对照在临时验证进程中分别移除纠正环、重新捕获基线、提前关闭 MCP、取消纠正上限或允许重复成功交付，同一组输入应分别拒绝应成功的修正或放过明确的错误结果。对照在实现后、代码审查前执行，不保留一次性脚本为产品测试。真实模型提示词对照需另行批准，不将受控 Agent 测试表述为真实模型成功率。

**待确认：** 是否批准本节 INITIAL 范围、固定两轮纠正和成功交付回执不可重放的边界。当前仅补充计划，尚未实现、提交、推送或部署。

## 11. 2026-11-12：同会话纠正的实现与边界修订

### 需求修订与取舍

用户质疑第 10 节通过错误类型判断停止的依据，随后要求“实现一下看看”。本次实施撤回 `correctable` 白名单，以及“命令失败、超时、非零退出本身足以停止”的提议。错误名称只能描述此次失败，不能证明 Agent 无法修复。保留 INITIAL 范围、原会话和原始验收基线，不扩展 REVISION 的持久恢复协议。

继续执行需要仍持有原 assignment 和 workspace allocation、原会话 ID，以及未过期的仓库能力。执行调用必须已经返回；adapter 现有的进程清理和 runtime 的租约取消机制继续生效。会话缺失、被拒绝或变更，任务/工作区归属丢失，能力失效或取消时不再续跑，也不另开会话回放已发生的编码。不能根据缺失的 session 推测一个可用会话；例如 Codex 首轮超时未返回 session 时仍无法续跑。

最多追加两轮是本版资源预算，耗尽只说明本次纠正额度用完，不说明问题不可修复。已有成功交付时，仍可以反馈诊断，但必须保持已确认 head；禁止再次交付或通过覆盖远端消除不一致。这替代第 10 节按远端错误分类直接结束的做法。

### 实际实现

- `WorkerExecutor._initial` 在同一个 MCP/schema 生命周期中完成 start、校验和有界 resume。原始 `VcsExecutionContext` 只捕获一次，纠正不创建新 job、attempt 或会话。校验返回后重新检查任务及工作区归属，续跑前重新检查 capability。
- 完成检查仍覆盖严格输出、backend、分支、祖先、工作区、原 remote 和远端 SHA；新增要求最终 head 同时匹配已确认的工具 outcome 和回执。缺少交付、校验命令失败，以及保留 session 的执行失败均反馈给原 Agent。worker 本身不修改或推送编码仓库。
- 纠正 prompt 传递阶段、安全诊断、Git/jj 原始锚点、目标和交付状态，要求满足原有测试和审查门禁。已有成功回执时禁止再次 push/交付；未知副作用需先核实再决定是否重试。未复制原始 schema 校验输入或 VCS stderr。
- `WorkerMCP` 在锁内保存首个成功回执，之后拒绝重复交付；锁覆盖验证和控制面调用，避免两个并发请求同时通过“尚无回执”检查。不新增通用 RPC 自动重放。
- Git 祖先查询区分 exit 1 的非祖先结果与其他命令失败，避免把缺失对象导致的 exit 128 错报成已证实的历史分叉。两类失败都可反馈，不据此分类是否可修复。

### 回归与必要性验证

先运行新增公共 `execute` 和 HTTP MCP 用例确认旧实现直接结束，随后使用受控 Agent、真实本地 Git/bare remote 和 MCP 检查修正结果。覆盖 amend 后恢复原基线的后继提交、严格输出、dirty tree、未推送、缺少交付、校验命令失败/超时和带 session 的执行超时。负例保留 amended sibling 或基线父提交，均不能通过祖先检查；成功回执后又提交/推送的结果也不能通过。补充会话缺失/变更/拒绝、能力过期、校验期间失去 assignment 和取消的边界验证。

临时对照只在独立 Python 进程中替换运行时行为，未改写共享工作树。对照脚本的审阅副本及日志位于 `.amp/in/artifacts/initial-correction-ablation-*`，脚本源保留为 `initial-correction-ablation-source.py.txt`；复现时复制为临时 `.py`，使用 `.venv/bin/python <script> <case>` 执行：

- `no-feedback`：预算设为 0，同一 amend 修复用例恢复为 exit 65，不能完成应成功的修正。
- `weaken-base`：把校验锚点替换为原基线父提交，错误历史越过祖先检查、失败阶段从 commit 变为 delivery，负对照检测到合同被放宽；该实验没有绕过剩余交付检查。
- `close-mcp`：提前关闭 MCP 后，修正会话交付出现连接错误，说明工具生命周期必须覆盖纠正。
- `increase-budget`：预算暂改为 4，持续失败确实调用 4 轮，固定预算用例失败。这个结果只证明预算约束有效，不证明 2 轮最优。
- `repeat-delivery`：移除成功回执保护后，重复 HTTP 调用再次到达控制面，回归用例失败。
- `concurrency`：受控调度下，两次并发 dispatch 在有锁/无锁时分别执行一次/两次交付，保留交付锁。

相关证据为 `initial-correction-before.log`、`initial-correction-ancestry-before.log`、`initial-correction-after.log` 和各 ablation 日志。全仓验证使用独立临时 PostgreSQL，命令为 `FLOWTOOL_TEST_DATABASE_URL=<isolated local PostgreSQL> .venv/bin/python -m pytest -q`，另运行 `.venv/bin/ruff check app tests`、`.venv/bin/mypy app tests` 与 `git diff --check`；最终结果见本线程[编码校验反馈](https://ampcode.com/artifacts/d89bcac2-580f-438e-b8f3-c0e84659bed0)。受控 Agent 验证不代表真实模型修复成功率，本次没有运行真实模型提示词对照。

### 交付状态

本次为本地实现及验证，尚未提交、推送或部署；没有热更新 worker、重跑 Flow 3 或修改其 journal。现有未归属本任务的工作树改动保持原样。发布 worker 与真实模型对照仍需单独执行，不作为本次已验证结果。
