# M1 单 Flow Issue→PR 闭环计划

> 状态：已批准并实现，真实 E2E 待执行 · 计划日期：2026-09-02 · 实施日期：2026-09-05 · M1 Feature 3/3
>
> 前置：Feature 1 已证明 CodeAgent 自主完成本地 VCS/commit/push；Feature 2 已校准 GitHub App、短时
> push capability 和 PR primitives。本 feature 只组装并验收单 flow happy path，不实现 Flow 派生，
> 但按[可派生 Flow 编排](../architecture/derivable-flows.md)先建立通用 Flow tool RPC，并固定首版“一 Flow
> 独占一个 worker execution slot 和完整 repo 直到终态”的边界。

## 1. 背景与目标

当前系统已经验证签名 Webhook、allowlist、持久 job、serve/worker 协议、affinity、lease/recovery 和结果
重放，但 INITIAL 仍使用占位 Issue 内容，Codex 结果只有 session/message/exit code，coordinator 固定以
`main` 为 base 并直接尝试开 PR。真实 GitHub、push、远端 ref 与状态转换没有共同验收。

本 feature 交付 M1 的首个真实产品闭环：

```text
allowlist 用户给 Issue 添加 flow label
  -> signed issues.labeled Webhook
  -> flow + INITIAL job
  -> control 读取真实 Issue/default branch并签发临时 push capability
  -> control 为 Flow 绑定空闲 worker slot；worker 使用该 Flow 独占 repo 启动 Flow tool broker 和 CodeAgent
  -> CodeAgent 自主改码、验证、commit、push
  -> CodeAgent 调用 flow.open_pr
  -> control 查询 GitHub 远端 ref、核对 commit并查找或创建 Flow 唯一 PR
  -> control 设置 review 元数据并返回 tool response
  -> worker 回传 session/branch/local commit/push/tool outcome
  -> attempt 保存执行事实，delivery 保存 branch/commit/PR
```

对用户而言，一次标签操作最终产生一张可人工审核的 PR，不需要手动复制 Issue、启动 Codex、执行本地
VCS 或直接调用 GitHub API。对系统而言，成功的权威不是 Codex exit 0，而是 fenced `flow.open_pr` 已由
control plane 执行、远端 branch SHA 与 worker 回传 commit 一致且 Flow 唯一 PR 已被持久化关联。

## 2. 范围与非目标

### 2.1 本次包含

- 真实 `issues.labeled` payload 到 INITIAL task 的单 flow 路径；
- Issue title/body、repository default branch 和目标 branch 注入；
- assignment-time push capability 的临时下发与 worker 生命周期；
- CodeAgent 自主 commit/push 的结构化 INITIAL result；
- worker-local MCP broker、版本化 Flow tool request/response 和 control-side command handler；
- 首个环境工具 `flow.open_pr`，以及 call fencing、幂等结果和安全重放；
- 远端 ref/SHA 强校验，未 push 或不一致时禁止开 PR；
- 按 Flow identity/head branch 查找或复用 PR，收口 tool/result replay 的重复开 PR；
- PR title/body/base、`Closes #N`、状态 label 和可选 reviewers；
- 一 Flow = 一隔离执行环境 = 一独占 repo = 一稳定 branch = 最多一 PR；
- Flow 从首次调度到终态独占单槽 worker；Attempt 只保存执行/session 事实并串行复用该 binding；
- worker 同 identity 在 recovery grace 内恢复原 binding；超过期限 Flow 明确失败且不迁移；
- CodeAgent 在 Flow 独占 repo 中以 `danger-full-access` 运行，不使用 `workspace-write`；同一可信 VM 可以承载
  多个使用不同目录的 worker；
- Flow 生命周期、job 执行状态和 PR delivery 状态分轴，以及明确失败原因；
- fake 外部端口的确定性自动化，以及一次 disposable repo 的真实 serve+worker+Codex E2E；
- README、架构状态、GitHub App/VCS 文档和本计划实施结果同步。

### 2.2 本次不包含

- M2 的 review settle、bub triage、Codex resume、revision/answer 或 thread resolve；
- M3 的人工 merge 检测、archive、repo cleanup 或 issue 收尾；
- M4 的完整 reconcile、并发同 flow 串行锁、base 前进/rebase、人工 push 和通用外部副作用 ledger；
- M5 的容器/微 VM、生产公网部署、通用多租户隔离或完整 egress firewall；
- 自动 merge、替目标仓库判断 CI 是否通过或自动批准 PR；
- worker 自动 clone 任意仓库；每个 worker slot 内的独占目标 repo 仍是部署前置；
- `flow.spawn/send/wait/inspect/accept/reject/cancel`、FlowLink、父级验收或 context/skill/artifact 分发；
- Flow checkpoint、跨 worker session/repo 迁移、在其他机器重建执行环境，或 Flow 等待期间释放 slot；
- 一个 worker 并发承载多个 Flow，或不同 Flow 共享 working tree、`.git/.jj` metadata。

M1 的“单 flow”是本次验收范围，不主动移除已实现的多 worker 调度，也不承诺多个真实 Codex flow 的
外部副作用并发语义；多个 Flow 只能分别占用不同的单槽 worker。通用 tool transport 在 M1 只注册
`flow.open_pr`；未注册的未来工具必须被 strict schema 拒绝，不能以空实现伪装成可用能力。

## 3. 协议、Flow tool 与 secret envelope

### 3.1 INITIAL task/result

版本化 worker 协议显式升级，避免在 v2 strict schema 上静默改变语义。INITIAL public task 至少包含：

- attempt id、repo 和 issue number；
- 真实 issue title/body；
- default base、目标 branch 和 remote identity；
- CodeAgent/VCS 完成条件。

INITIAL result 至少包含：

- Codex session id、exit code 和用户可读总结；
- Agent 声明且 worker 独立验证的 VCS backend、目标 branch、本地 commit SHA；
- push outcome、成功的 `flow.open_pr` call id 和返回的 PR handle；失败时为稳定阶段和脱敏消息。

worker 读取 VCS 事实形成 result，但不替 CodeAgent commit/push。result 不包含 credential、完整 Codex
JSONL 或任意环境变量。

### 3.2 Flow tool RPC

v4 增加可复用的 tool request/response message family 和分类 error/retry metadata。它与 assignment 的终态 result 并行存在，允许
CodeAgent 在一次 run 尚未退出时调用 control plane 能力。M1 只注册：

```text
flow.open_pr(title, body) -> {pr_number, url, head_sha, delivery_version}
```

repo、installation、head branch、base 和调用者 Flow 由 control plane 从 assignment 解析，不能由模型自由
指定。worker-local MCP broker 接收 Codex tool call，通过 `WorkerClient` 的现有认证 WSS 复用连接发给
gateway；worker 附加 flow/job/assignment/epoch/call id，control 做 fencing 后才执行。MCP broker 不持有
Store、GitHub client、installation secret 或 worker bearer token。

每个 call 先持久化，再执行 GitHub effect；稳定 call id 和 Flow 唯一 delivery 共同保证断线、Codex 重试、
worker result replay 或 serve 重启不会创建第二张 PR。同一个 call 的 response 可重放；相同 Flow 的新 call
只能复用并验证已有 PR，参数冲突时 fail closed。

这个 transport 是未来 `flow.spawn/send/wait/inspect/accept/reject/cancel` 的共同接缝，但这些 command、
FlowLink、context/skill manifest 和父级 continuation 不属于 M1。

### 3.3 临时 push capability

push capability 放在 assignment 的专用 secret envelope，不混入持久 job payload或可重放 result：

- coordinator 在即将发送 INITIAL assignment 时按 flow installation/repo 签发；
- 只通过已认证 WSS 连接发送；开发态明文 `ws://` 必须继续显式 opt-in；
- worker journal 只保存 job/token/state/digest，不保存 task 或 capability；
- worker 将 capability 暂时暴露给 Git credential mechanism，使 CodeAgent 能执行 push；
- run 结束、超时或取消后立即清理 helper/环境引用；
- gateway、protocol validation 和错误日志不得打印完整 assignment JSON；
- assignment 尚未 Started 而被重新派发时签发新 capability，不复用过期 secret。

由于 CodeAgent 必须执行 push，它原则上能使用该 capability。M1 通过独占 repo、短 TTL、GitHub-only
网络目的地、allowlist 和 branch protection 限制风险；这些措施不是多租户安全隔离，文档不得宣称
secret 对执行代码不可观察。

## 4. 状态分轴与副作用顺序

### 4.1 Flow、execution binding、Attempt 与 delivery

M1 把“一 Flow 一执行环境/repo/branch、最多一个 PR”写成领域和数据库约束：

- Flow 是执行与交付身份；每个 Flow 只有一个稳定 branch、一个独占 repo identity 和零或一个 PR delivery；
- `FlowExecution` 保存 Flow 到 worker slot/repo identity 的生命周期 binding；每个 worker 同时最多一个
  active binding，直到 Flow 终态清理后才释放；
- Attempt 是同一 Flow 的执行、恢复或 retry 事实，保存 session/run 并引用 binding，不再拥有 worker、repo、
  branch 或候选 PR；
- job 状态继续表达 queued/running/done/failed；
- PR 的 none/opening/open/closed/merged 状态保存在 Flow-owned delivery，不再复用 `FlowState`；
- Flow 只保留 active/succeeded/failed/cancelled 一类生命周期事实，面向用户的 `CODING/PR_OPEN/REVISING`
  由 job、delivery 和 review 数据派生。

M1 不增加 waiting/acceptance 状态；未来分别由 `FlowWait` 和 `FlowLink` 承载，避免重新制造组合枚举。
PR 已打开但 Flow 仍 active 时不释放 execution binding；这为后续 review/revision 保留同一 repo。

### 4.2 Prepare

INITIAL prepare 按以下顺序：

1. 读取 flow，确认仍处于可执行状态且 installation id 存在；
2. 原子创建或读取 `FlowExecution`：没有 binding 时只选择空闲单槽 worker，绑定其独占 repo identity；
3. 创建或复用 Flow delivery，确定基于稳定 Flow ID 的唯一 branch `flow/<flow-id>`，再创建本次 attempt；
4. 读取真实 Issue snapshot 和 repository default branch；
5. 构造 public task；
6. 临近 assignment 发送时创建 push capability；
7. 只把 job 发给 binding 指定的 worker；Started 时验证 worker identity/epoch 和 repo identity，运行态由
   running job 派生，不修改 PR delivery。

Issue/repository 读取失败不创建可运行 task；权限/配置错误转明确 FAILED，暂时错误按现有 job retry 能力
保守处理，不在本 feature 引入无限重试。

worker 断线时只等待同 identity 在 recovery grace 内恢复原 repo。期限结束后 Flow 进入明确失败；M1 不把
job 改派到另一 worker。terminal cleanup 前 binding 和 repo 都保留，且不能承载其他 Flow。

### 4.3 `flow.open_pr` 与 Apply result

`flow.open_pr` command 的成功顺序：

1. fence tool call，读取 Flow/attempt/delivery，确认 assignment 仍可提交结果；
2. 查询固定 branch 的 GitHub remote ref，并把 remote SHA 保存为 delivery head；
3. 按 Flow identity + head branch 查找已有 PR；存在则验证 base/head 并复用，不存在才创建；
4. 原子保存 PR number、head SHA、tool response 和 delivery=`open`；
5. 幂等设置状态 label，reviewers 非空时请求 review；
6. 触发一次 `on_pr_opened` 并把可重放 response 返回 CodeAgent。

INITIAL terminal result 随后校验 kind、attempt/worker affinity、exit code、session、branch、本地 commit SHA、
push outcome、tool call id 和 PR handle。只有本地 commit、remote head、tool response 与 Flow delivery 全部一致，
job 才进入 DONE；attempt 保存 session/run 事实，不复制 PR ownership。

PR body 包含 `Closes #<issue>` 和 CodeAgent 总结；base 使用仓库 default branch，不硬编码 `main`。

### 4.4 最小幂等与失败

tool request 可能在“GitHub 已开 PR、response 尚未返回”的窗口重放。持久 call record、Flow delivery 唯一
约束和按 head 查询共同收口窗口。GitHub create 返回 validation/conflict 时再次查询 head，只有确认唯一
匹配 PR 才继续；歧义时 fail closed。之后的 terminal result replay 只核对已经记录的 tool outcome。

以下情况禁止开 PR并使 job/flow 明确失败：

- 缺 installation、Issue/repository 或 capability；
- CodeAgent 非零退出、无 session、无 commit、未 push；
- result branch 不等于 Flow delivery branch；
- remote branch 不存在或 SHA 不一致；
- 已有 PR 的 head/base 不匹配；
- tool call 越权、过期、fencing 失败或与已有 delivery 冲突；
- GitHub auth/permission/validation 失败。

能安全评论时在原 Issue 留脱敏原因；评论失败只记结构化日志，不覆盖最初失败。若 PR 已创建而 Agent 随后
异常退出，delivery 保持 open、job/attempt 记录失败，显式 retry 复用同一 PR。M1 不删除已推送 branch，
保留现场供诊断和显式 retry。

## 5. 模块与文件布局

- `app/protocol.py`
  - 升级协议版本，扩展 INITIAL task/result、tool request/response 和 assignment secret envelope；
  - strict parsing、secret-safe repr/logging 与版本拒绝。
- `app/control/coordinator.py`
  - 创建或复用 Flow execution binding、真实 Issue/default branch prepare；
  - terminal result 与已持久 tool outcome 的最终核对，Flow 非终态时不释放 binding。
- `app/control/tools.py`（新增）
  - Flow tool registry、fencing/idempotency、`flow.open_pr` command 和 find-or-open PR 顺序；
  - 为未来 Flow command 保留通用 dispatch，不注册未实现能力。
- `app/control/gateway.py`
  - assignment 临发 capability 获取、tool request/response multiplex 和不落盘保证；
  - 只向 binding 指定的 worker 派发；不在日志中序列化 secret envelope。
- `app/worker/tools.py`（新增）/ `app/worker/client.py` / `app/worker/executor.py`
  - 启动 worker-local MCP broker，把受约束 tool call 接到 WorkerClient；
  - 校验 Flow/repo binding，使用该 Flow 独占 repo，管理 capability 生命周期并形成结构化 result；
  - journal/recovery 保持只记录非 secret 数据。
- `app/store.py` / `app/models.py`
  - 增加 Flow 唯一 execution binding、delivery 和 tool call/result ledger；Attempt 收回为执行事实；
  - 约束一个 worker 同时最多一个 active Flow binding，并保存 recovery/terminal cleanup 所需状态；
  - 分离 Flow 生命周期与 PR delivery 状态，必要 schema migration 保持向前兼容；
  - 不增加 token/credential 列。
- `app/codex_driver.py`
  - 为本次 run 注入 Flow MCP server 配置，不把 worker/control credential 放进 Codex 环境。
- `app/context_builder.py`
  - 注入真实 Issue/base/branch/remote 和 CodeAgent-owned VCS 完成条件。
- `app/config.py` / `env.example`
  - reviewer、status label、目标 remote/network policy 的 M1 配置与启动校验。
- `tests/`
  - protocol、gateway、coordinator、executor、Store、ASGI remote E2E 和 secret 负向测试。
- `docs/`
  - 更新当前实现状态、M1 验收记录、运行前置和仍属 M2+ 的边界。

## 6. 实施顺序

1. **领域不变量与 Store schema，测试先行**
   - 先写一 Flow 一 binding/repo/branch/delivery、一个 worker 一 active Flow、Attempt 仅属同一 Flow 和状态
     分轴测试；
   - 增加 execution binding、delivery 与 tool-call ledger，完成向前 schema migration。
2. **协议与 Flow tool transport，测试先行**
   - 写 v4 parse/reject、INITIAL 字段、secret envelope、fenced tool request/response 和未知 tool 拒绝测试；
   - 实现 WSS multiplex、worker-local MCP broker 与 control tool registry。
3. **真实 prepare，测试先行**
   - fake GitHub 返回 Issue/default branch/capability；
   - 覆盖空闲 slot 分配、同 Flow 复用、跨 Flow 拒绝共享、缺 installation、非 `main` base、空 Issue body、
     签发失败和同 worker 重派发刷新 capability。
4. **Worker capability 与 VCS result，测试先行**
   - 注入临时 credential、运行 CodeAgent、读取 VCS outcome、清理；
   - 覆盖成功、exit failure、无 commit、push failure、timeout、异常清理和日志脱敏。
5. **`flow.open_pr` 与 GitHub 副作用，测试先行**
   - remote SHA mismatch 必须在 open PR 前失败；
   - 覆盖无 PR、新建 PR、已有匹配 PR、call 重放、create 后断线、越权和歧义 PR。
6. **入口到远程 worker 的确定性 E2E**
   - signed Webhook -> flow/job -> gateway -> fake CodeAgent 调 tool -> control GitHub -> delivery open；
   - 覆盖 allowlist 拒绝、delivery 去重、tool/result replay、Flow binding 保留和 serve restart 后
     response/apply。
7. **真实 M1 E2E 与文档**
   - 用户授权 disposable repo、App installation 和测试 Issue；
   - 启动 PostgreSQL、serve、一个 worker 和真实 Codex；
   - 标签触发后验证 commit/PR/Store/CLI；
   - 同步 README、架构状态、roadmap、diagram 和本计划实施结果。

## 7. 测试策略

### 7.1 自动化

- router：真实形状 `issues.labeled` payload、sender/bot/allowlist/delivery 去重；
- protocol：旧版/v4 边界、strict secret envelope、tool request/response fencing、分类错误与 result branch/SHA 验证；
- gateway：capability 只在 assignment 内存/WSS，tool RPC multiplex，重派发刷新，不进入 result inbox；
- Flow tools：未知工具拒绝、越权/过期 call 拒绝、call response 重放和一 Flow 一 PR；
- MCP broker：Agent只能看到已注册工具，broker 无 GitHub/Store/worker bearer credential；
- journal：received/started/terminal/replay 文件都不含 capability 或 Issue 外的 secret；
- coordinator：真实 context、default branch、terminal result/tool outcome 核对和状态顺序；
- scheduler/gateway：首次只占用空闲 worker、同 Flow 只回原 worker、已有 binding 时其他 Flow 不可领取；
- executor：repo identity 与 Flow binding 一致、CodeAgent ownership、credential cleanup、结构化 VCS result；
- Store：一 worker 一 active binding、Flow delivery、tool ledger 和 result replay 不重复 PR；
- remote E2E：两个真实进程边界配 fake 外部端口，最终 delivery=`open`；
- recovery：同 identity 在 grace 内接回原 repo；超时后 Flow 失败，不产生跨 worker assignment；
- regression：多 worker 各自单槽调度、lease/recovery、serve restart、CLI 和 M0 Webhook 全部保持。

### 7.2 真实验收

在 disposable repo 创建一个小而确定的 Issue，由 allowlist 用户添加 `flow` label：

1. Webhook 返回 202，数据库只有一个 active flow 和 INITIAL job；
2. worker 接收真实标题/正文/default branch 和 `flow.open_pr`，CodeAgent 自主完成改码、测试、commit、push；
3. CodeAgent 调用环境工具后，由 control plane 查询远端、创建或复用 PR并返回 handle；
4. GitHub 远端 branch SHA、worker result SHA、tool response 和最终 PR head SHA 一致；
5. PR base 是真实默认分支，body 包含 `Closes #N`，reviewer/label 符合配置；
6. flow lifecycle=`active`、delivery=`open`、attempt 保存 session、job=`DONE`，execution binding 仍占用该
   worker/repo；
7. 重放同一 tool call 或 terminal result不产生第二张 PR或重复 tool effect；
8. PostgreSQL、journal、Flow repo、日志和测试产物搜索不到 installation token/private key；
9. 人工确认 PR diff 和 CodeAgent commit message 均来自本次任务。

真实验收记录 App/repo/Issue/PR 编号、commit SHA、Codex CLI 版本、开始结束时间和检查结果；不复制 token、
private key、完整 prompt 或完整会话日志。

常规验证命令：

```bash
FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool:flowtool@127.0.0.1:5432/flowtool uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv lock --check
uv build --no-sources
uv run flowtool --help
git diff --check
```

## 8. M1 完成定义

只有同时满足以下条件，README/架构状态才能从“M1+ 未验证”更新为“M1 已验收”：

- Feature 1/2 的真实 smoke 已完成，且对应计划记录实际结果；
- 本 feature 的全部自动化与真实 Issue→PR E2E 通过；
- CodeAgent 是 commit/push 的决策者和执行者，worker 没有事后自动提交；
- CodeAgent 通过 `flow.open_pr` 请求交付，只有 control plane 持有并调用 GitHub PR/label/reviewer API；
- 一 Flow 一 execution binding/repo/branch/PR、一个 worker 一 active Flow、Attempt 只表示执行尝试已成为
  持久约束；
- PR open 后 Flow 非终态且 binding 仍保留；worker loss 超过 grace 后失败而不是迁移；
- remote SHA 验证和按 Flow/head PR 复用已覆盖 tool/result 重放场景；
- Flow tool transport 不绑定 `open_pr` 特例，未来 command 可通过 registry 扩展；
- secret 生命周期的负向测试和产物检查通过；
- 文档明确 M2 review loop、M3 merge/archive、M4 reconcile/冲突和 M5 隔离仍未完成。

## 9. 风险与回退

- **前置 feature 未达到真实验收**：F1/F2 任一只有 fake test 时不得开始真实 M1 E2E，也不得降低完成定义。
- **secret 经协议泄漏**：任何 Store/journal/log 泄漏阻断 PR；删除产物、撤销 token 并修复后重跑。
- **tool RPC 扩大 M1 面积**：先实现单工具、单 in-flight call 和 strict registry；不顺手加入 spawn/wait。
- **单槽利用率低**：Flow 没有 running job 或 PR 已打开时仍占用 worker；这是首版用资源换取无 checkpoint
  一致性的明确取舍，后续在可恢复执行完成后再优化。
- **worker 丢失即 Flow 失败**：grace 内只允许相同 identity 恢复原 repo；M1 不假装能在另一台机器续跑。
- **重复 PR 窗口**：以稳定 Flow branch、持久 call record 和 find-by-head 收口；歧义时人工处理，不猜测归属。
- **Codex 产出不可靠**：remote SHA 是硬门；prompt 或 final message 不能替代事实检查。
- **目录不是安全边界**：M1 允许多个 worker 在同一可信 VM 的不同独占目录运行，但
  `danger-full-access` CodeAgent 原则上可能访问同一 OS account 可见的兄弟目录。M1 只能用于同一信任域，
  host 不得暴露 control-plane credential 或域外敏感数据，也不能宣称生产多租户安全；强隔离属于 M5。
- **默认分支/base 前进**：M1 取 prepare 时的 default branch，不自动 rebase；冲突/base drift 以失败暴露，
  后续 M4 处理。
- **外部服务瞬时故障**：保留明确 retryable reason 和显式 `flow retry`；不在本 feature 引入无限后台重试。

回退时可关闭真实 GitHub/Codex 配置并继续运行已验证的 M0/远程 worker 基础设施。已创建的 branch/PR
不自动删除，由操作者在 GitHub 审核后处理；数据库保留失败 flow 供诊断。

## 10. 实施结果（2026-09-05）

已实现本计划的领域、协议与执行路径：`FlowExecution`、`FlowDelivery` 和 `FlowToolCall` 分别持有生命周期
binding、交付状态和幂等调用账本；调度器在 Flow 终态前保留单槽 worker/repo；INITIAL 使用真实 Issue、
default branch 和临发的短时 push capability。Codex run 获得只注册 `open_pr` 的 worker-local STDIO MCP，
请求经现有认证 WebSocket 送到 control。Worker 在调用前验证本地 branch/commit/clean/remote，control 再以
GitHub ref 核对 SHA，随后按稳定 head find-or-open PR；terminal result 还会与持久 tool outcome 再核对。

自动化结果：pytest 95 passed、22 skipped；Ruff、Mypy 与构建通过。22 个 skip 是需要 PostgreSQL 的 Store 场景，
当前环境没有 Docker/PostgreSQL，未执行这些集成测试。尚未提供用户授权的 disposable GitHub repository，
因此真实 GitHub App smoke 和本 feature 的真实 Issue→PR E2E 未执行；依据 §8，M1 仍不能标记为“已验收”。
