# FlowTool（代号，未定名）

FlowTool 支持从带 `flow` 标签的 GitHub Issue 或直接输入的任务内容开启
`code agent 编码 → PR → 人工审核 → 自动回喂修订 → 人工合并 → 归档` 的 headless 闭环。
工作流交互面包含 GitHub Issue、PR 和 [Web 控制台](app/controler/web/README.md)，通过同源 API
展示真实 Flow、执行记录和持久时间线，由 Cloudflare Access 限制单 owner 访问。
Flow 使用 `queued/running/review/blocked/finalizing/archived` 六态；看板将 finalizing 合入进行中。
归档结果为 `succeeded/abandoned`，无 PR 的调查可凭非空结论成功结束；显式恢复开启同一 Flow 的新轮次。

代码按职责分为 `domain`、`contracts`、`persistence`、`github`、`controler`、`worker`、
`telemetry` 与 `plugins`。`controler` 是控制服务端，内部按事件、Worker 管理、工作流、审核
和远端对账归组；目录和依赖边界见[组件职责](docs/architecture/components.md#包依赖边界)。

> 当前状态（2026-10-15，核对最新 main）：**M0 已验收；M1–M4 功能及 M5 部署、接收、运维代码已落地，真实 E2E 与部署验收仍待完成**。
> Control 运行 Judge 并管理 GitHub 操作，Worker 执行编码、修订和归档清理。Runtime 管理共享
> Database，各服务自行创建所需仓储；Webhook 在 receipt 事务内创建服务与路由。
> 实现与验收边界见[架构状态表](docs/architecture/README.md#当前实现状态)，最近装配调整见
> [持久层简化记录](docs/plan/persistence-simplification.md)。

## 当前能力

- **直接创建 Flow**：看板顶栏点击“新建 Flow”，选择 Project（GitHub 代码仓库），输入任务内容即可开启，
  不要求 Issue 或 webhook。项目来自 GitHub App 授权清单，零历史仓库也可选；提交时重新校验访问与归档状态。
  任务内容以字符串原样保存，最多 32 KiB UTF-8，不要求填写标题。
  首轮准备时异步推理标题，不等待生成结果；生成前或失败时依次使用 Issue 缓存标题、PR 标题、`Flow #ID`。
  原始输入用于首轮、修订与审核；无 Issue 时不查询 Issue、不添加关闭指令，也不参与 Issue 型 Projects 同步。
  提交结果未知时先刷新核对，页面不会自动重发；没有空闲 Worker 时正常排队。

- **准备期读重试**：Assignment 发出前的临时 GitHub 读取故障按原 worker binding 自动退避；
  `FLOWTOOL_PREPARE_MAX_FAILURES` 默认 5 次，`FLOWTOOL_PREPARE_RETRY_WINDOW_SECONDS`
  默认 900 秒，遵守 Retry-After，重启保留次数与时间。`flow show` 展示准备失败、下次时间和原因。
  超出预算或永久 API 错误走现有失败/显式 retry 路径；凭据签发和 worker 执行不在此重试范围。

- **关闭与重开**：当前未合并 PR 关闭进入废弃收尾；停止、结果和已发操作结清后归档，
  放弃/调查保留工作区。已合并 PR 立即成功归档，原 worker 独立停止并回收工作区；
  资源未结清时仍拒绝同一 Flow 的 resume。PR/Issue 重开不自动恢复。
  未合并 Flow 的失败与未知执行进入 blocked，仍占用该 Issue；`flow retry` 不绕过执行、结果和停止门槛。
  只有 `flow resume` 开启新轮次、新分支与新交付，旧 PR 和旧反馈不能授权新工作。
  INITIAL 无 PR 失败也保留执行绑定，结束时须由原 worker 确认停止并清理后归还工作区；
  原 worker 离线时继续等待，不提前归档。恢复分支使用 `flow/<id>-round-N`，保留首轮 `flow/<id>`。

- **反馈对账**：原 Issue、PR 普通评论和 Review 正文分别保存分页进度，默认每 300 秒复扫；
  每个来源每次至多读取 50 条完整正文，未完成的页 5 秒后继续。重启保留 cursor/退避，
  `flow show` 展示各来源诊断。旧命令不追溯执行，列表缺席不推断删除。
  恢复后的原 Issue 评论按创建时间隔离，补拉和 webhook 使用相同边界；编辑旧评论不转入新轮。
  新轮开始同秒及缺少创建时间的评论不进入反馈批次，可在下一秒重新发新评论；历史仍可阅读。
  边界取本轮首个 INITIAL 的持久创建时间，不随 retry 改变；controller 必须保持时钟同步，
  此规则不补偿与 GitHub 的时钟偏差。新 PR 评论与 Review 不受原 Issue 的时间边界限制。
  行内线程同样持续对账，包括人工 resolved/unresolved/outdated 事实；每次读取一个线程的
  至多 20 条评论，嵌套分页可恢复。跨页片段不作为完整 resolve 依据，poll 不执行远端写操作。

- **PR 对账**：对已追踪 PR 定时查询并补偿漏失合并事件，检查进度与退避持久化；
  `FLOWTOOL_RECONCILE_INTERVAL_SECONDS` 默认 300 秒，`FLOWTOOL_RECONCILE_BATCH_SIZE`
  默认 64，`FLOWTOOL_RECONCILE_CONCURRENCY` 默认 4。`flow show` 展示最后成功和下次检查时间。

- **恢复并发**：lease、settle 和网络结果恢复独立运行；结果恢复默认 4 并发、每页 64 项，
  可用 `FLOWTOOL_RESULT_RECOVERY_CONCURRENCY`（1–64）和
  `FLOWTOOL_RESULT_RECOVERY_BATCH_SIZE`（1–1000）调整。有限扫描周期保证旧待确认结果可重试。

- **接收与持久化**：校验 GitHub Webhook 的 `X-Hub-Signature-256`，处理 delivery 去重、allowlist、
  触发、取消和 review 入库；PostgreSQL 是唯一运行时数据库，启动时初始化 schema。
- **远程执行**：`flowtool serve` 管理状态、数据库和 GitHub 操作；`flowtool worker` 主动通过认证
  WebSocket 连接，执行 INITIAL、REVISION、ARCHIVE 任务；Judge 在 control 本地运行。
  支持多 worker 公平调度，一个 worker 可在不同 workspace 同时执行多个 job。
  每 worker、每仓库的 workspace 上限默认 2，可配置为 3；按等待任务自动补充并登记具体目录。
- **执行环境与恢复**：Flow 独占一个 worker 上的具体 workspace，Attempt 保存本次运行和 session；
  assignment 使用 worker/workspace/generation/epoch/job/token 校验，逐 Flow journal 独立恢复。
  未授权派发的任务可在租约到期后重排，已授权
  派发但缺少 Started 的未知执行不按未发送重排；Started 任务在
  recovery grace 内等待原 worker；worker journal 支持忙碌重连、serve 重启恢复与终态结果重放。
- **Issue→PR 交付**：读取真实 Issue 和仓库默认分支，为 Codex 提供单仓库、短时 Contents write
  push capability，由 agent 自主 commit/push。`flow.open_pr` 先核验远端 SHA 并持久化 PR 意图，
  **INITIAL 终态结果校验成功后**才创建或复用唯一的 open PR，再设置标签和 reviewer。
  已修复失败 Flow 重试后的执行绑定复用和提前开 PR；创建后已关闭的 PR 保留身份并暂挂，
  INITIAL 结果只收尾执行事实，不回开 PR。
- **原子提交与操作恢复**：trigger/retry/merge/cancel 的状态和任务变更在共享事务内提交；
  终态结果先持久接收，再执行带回执的 GitHub 操作，最后原子提交本地结果并回复 `server.accept_result`。
  恢复时核对远端状态，避免重复派生 job 或重复发送评论；无法确认的远端结果保留待核对状态。
- **内部错误**：准备、workspace、执行、交付校验、归档及线程映射等内部错误仅保留在任务/反馈诊断、
  控制台和日志中，不发布 GitHub 评论。旧版尚未发出的失败评论也跳过；发送结果不明的记录只查询确认，
  不补发或删除。正常交付摘要、业务反馈回复和成功归档通知继续发布，失败状态与显式 retry 规则不变。
- **合并与归档**：Control 核验当前 PR 合并后，在同一事务保存合并事实、原 Worker 停止请求、
  本地通知意图和带轮次的 archive activity，立即进入 archived/succeeded。原 worker 离线、
  绑定缺失或已释放、清理或通知失败均不阻塞该结论。原 worker 确认执行已退出、远端结果及
  已发操作结清后，Control 释放原绑定并推进 generation，正常路径不等待 recovery grace。
  下一 INITIAL 由 worker 在独占目录后清理旧工作副本；库存不授权释放，ACK 不授权复用。
  通知独立更新 Issue/PR 标签和评论，
  不声称目录已清理；评论失败可用 `flow retry` 补发缺项，结果不确定时只查询恢复。
  已保存完整合并事实的旧积压有界恢复，open 的执行失败 Flow 不变。
  迟到证据能证明归档前已合并时自动更正为成功，永不恢复执行。
- **GitHub 请求超时**：App 和 installation 客户端的连接、读写及连接池等待默认各为 30 秒，
  可通过 `FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS` 调整；这不是整批结果恢复的总时限。
- **持久反馈批次**：反馈版本与 settle deadline 入库，重启后恢复；同 Flow 串行处理冻结批次，
  运行中新评论/编辑进入下一批。仅 allowlist 作者提交的 Request Changes 经 grace 合批直达修订；
  包含 `@bot` 的评论也进入 Judge。wait 等待新信息，
  answer/ignore/明确 addressed 只消费本批次精确版本，未决问题保留。
- **语义决策与直接答复**：control 冻结 Issue/PR、有限 patch 和讨论，通用推理路由生成答案，
  推理作为 `INFERENCE` Job 进入统一分配流程；review 输出校验后才成功，只有 revise 创建编码 Job。
  GitHub 回复仍由 control 应用并按批次 marker 对账。control 在 bub 配置及依赖预检通过后管理
  一个仅提供 `worker.infer` 的 mini worker；其就绪、空闲且能力匹配时优先领取推理 Job。
  bub 未配置、半配、忙或不合条件时，选择普通 worker 的独立推理槽，复用其 Codex/Amp。
  健康检查不调用模型；实际调用失败按分类处理，永久错误不会因换 provider 而自动重试。
  总预算包含排队、通信与退避，最多授权派发 3 次（含首次、结果未知的派发），次数和截止跨重启保留。
  可恢复错误耗尽后保留 READY，后续扫描可创建新一轮 Job；
  非可恢复错误及不完整快照保守 WAIT，新有效反馈仍按原规则触发新批次。
  三次均失败或提前耗尽预算，在详情“执行记录 → 推理通知”保留安全摘要，不发送 GitHub 通知。
- **运维入口**：CLI 可查看 Flow、交付分支/PR、worker、任务恢复状态和未完成的 GitHub 操作，
  并提供 cancel/retry 命令。已核验修订中被明确拒绝的通知可补发；结果不确定时拒绝直接 retry。
- **取消与 drain**：cancel 持久化停止意图，worker 取消当前执行并等待其 Codex/VCS
  进程组退出后发送停止凭据，不在 stop 中执行 Git 清理；下一 INITIAL 独占后整理旧工作副本。
  停止凭据、远端结果及已发送 effects 未结清前不释放绑定，ACK 只确认 journal。
  停止只影响目标 job；合并后的原位置在精确停止回执结清后可立即分配给下一 Flow。
  drain 停止分配新 Flow，已有绑定继续收尾。未知旧执行保持阻塞，
  无强制释放入口，见[取消与停机](docs/operations/cancel-drain.md)。
- **Server 镜像更新**：独立更新器监听仓库 Webhook，验签后持久写入主机 JSON 队列，后台核验发布、
  候选镜像和删列检查，再缩至零、等旧 Pod 正常退出、以固定 digest 恢复一个副本并核验 Ready/imageID。
  同镜像中断可直接续作，目标已 Ready 不重复停启；不直连集群 PostgreSQL，不暂停全局业务，
  不等待 Worker 更新或改变 recovery grace，见 [Server 更新](docs/deployment/server-update.md)。
- **Worker 原地更新**：正式 uv Git 安装可用 `flowtool worker --update` 自动查询最新发布，更新
  现有包和依赖，经过新进程版本/CLI/依赖核验后退出；操作者先停旧进程，成功后按原方式启动。
  不创建新环境或版本目录，不增加源码运行跳过分支，见 [Worker 更新](docs/deployment/worker-update.md)。
- **Projects 与可观测性**：可选组织 Projects v2 同步独立运行；结构化日志、受 token 保护的
  `/metrics`、数据库就绪探针 `/readyz` 与 worker 状态文件已接入。Docker/Compose、Kubernetes
  和常驻运维说明见[部署指南](docs/deployment/README.md)，不代表真实环境已验收。
- **可靠修订**：每轮更新单仓库 push 凭据，复用原 repo/branch/PR；worker 核验 Git/jj 工作区及远端，
  control 再核验 PR/head/base/ref 与提交关系。缺 session 或明确未执行的 session 拒绝仅兜底一次，
  journal 防止中断后盲目重跑。未决反馈保留；no-op 或部分完成不重新请求 review。

- **原线程反馈闭环**：control 冻结有界分页的线程/comment 身份，按线程合并 addressed/deferred 说明；
  处理说明确认发送后，检查完整人类反馈覆盖、当前 PR/head 和较新的本地版本，再 resolve 并复读。
  回复/resolve 响应丢失由持久账本恢复；新评论进入下一批，权限或映射问题保留诊断并暂停 re-review。

## 尚未完成

- M3 F1/F2/F3 已合并，完整真实闭环仍待验收，见
  [#26 计划与实施记录](docs/plan/m3-archive-status.md)。
- 真实 GitHub App smoke，以及授权的一次性仓库中的完整 Issue→PR E2E 仍待执行；
  自动化测试使用外部服务替身，不代表真实 GitHub、Codex、bub 集成已验收。
- bub adapter 已通过真实库加本机假模型端点的契约测试；真实 provider 语义 smoke 仍待执行。
  review thread 映射、回复和 resolve 已通过自动化；冲突处理和真实多轮闭环仍待验收。
- 已追踪 PR 的 merge/close/reopen 及评论/线程漏投补偿已实现；漏失的新 Flow trigger 不由对账补建。
- 真实取消/恢复演练、镜像发布与目标集群验收仍待完成。跨 worker session 迁移、
  多 serve 高可用、冲突自动处理、GitHub-only 网络限制和每 Flow 动态强隔离仍属后续工作。

详细边界见[架构状态表](docs/architecture/README.md#当前实现状态)；M2 合并记录与剩余验收见
[M2 状态与待办](docs/architecture/README.md#m2-状态与待办)和 [F4 实施记录](docs/plan/m2-review-resolution.md)。

## 文档

- [Flow 会话查询 API](docs/architecture/conversation-api.md)：control 代理 worker 读取 Codex 历史，供 WebUI 分页展示；
- [已取消 Flow 手动归档](docs/plan/cancelled-flow-archive.md)：归档入口、持久化与取消/恢复的安全边界；
- [`docs/architecture/`](docs/architecture/README.md)：当前实现状态、目标架构、组件、流程、决策和接入说明；
- [`docs/architecture/diagram.html`](docs/architecture/diagram.html)：手工维护的交互式架构可视化；
- [`docs/plan/`](docs/plan/README.md)：计划、批准状态和实施结果记录。

## 平面边界

- **Control plane**：Webhook、状态机、持久化和 Judge，独占数据库操作与 Flow 的 PR 创建、标签和 review 编排；
- **Worker execution plane**：prompt、Codex 和 Flow 独占 repo，通过出站 WebSocket 接收结构化任务，
  Git/gh 使用当前 assignment 的单仓库短时 installation token；不持有数据库、GitHub App 或 Judge 凭据；
- **本地执行**：Flow repo 负责目录所有权；只有运行在无法读取 control-plane secret 的专用 OS account、
  container 或 VM 中的 worker 才能显式开启 full access。Agent 自选 Git/jj，worker 只做结果核验。

VCS 不是端口；详细设计与当前限制见 [`components.md`](docs/architecture/components.md) §4.6。

## 快速开始

完整步骤见 [部署与 Cloudflare 接入指南](docs/deployment/README.md)。

要求 Python 3.12+、uv，以及可访问的 PostgreSQL；以下用 Docker Compose 启动开发数据库。
实际编码还需要在隔离的 worker 环境准备 Codex CLI 和认证，GitHub App 配置见
[接入说明](docs/architecture/github-app.md)。

先准备 control plane：

```bash
uv sync --locked --dev
docker compose up -d postgres
cp env.example .env
# 编辑 .env：配置 GitHub App、allowlist、reviewers 和 worker 身份/token
FLOWTOOL_HOST=127.0.0.1 uv run flowtool serve
```

这里的 `uv sync` 是源码开发安装，不支持 `worker --update`；该命令仅用于符合[安装要求](docs/deployment/worker-update.md#首版支持的正式安装)的 uv Git 虚拟环境。

另开终端启动本地连接验证用的 worker；下面的 token 必须与 serve 的 `FLOWTOOL_WORKER_TOKENS` 一致。
该示例未授权 Codex 编码执行：

```bash
FLOWTOOL_WORKER_ID=worker-1 FLOWTOOL_WORKER_TOKEN=change-me-worker-token \
FLOWTOOL_SERVE_WS_URL=ws://127.0.0.1:8000/internal/workers/connect \
FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true uv run flowtool worker
```

实际编码需安装 `git`、`gh` 和所选的 Codex 或 Amp CLI（使用 jj 时另装 jj）。默认 `FLOWTOOL_CODING_AGENT=codex`；
专用 worker 可改为 `amp`。`FLOWTOOL_AMP_RUN_TIMEOUT_SECONDS` 和
`FLOWTOOL_CODEX_RUN_TIMEOUT_SECONDS` 都是编码空闲超时秒数，默认 1200，必须为正有限值。
直接 CLI 的 stdout/stderr 每读到非空新数据就重新计时，半行、工具调用/结果和进度也算；
持续有输出可以超过 20 分钟。心跳、历史查询、EOF 和进程存活不续期；静默工具仍可能超时，
持续诊断日志也会续期。长任务可能超过 GitHub capability 有效期，凭据不自动刷新。
Amp 使用该 worker
专用 HOME 的原生 `amp login`，或可选 `AMP_API_KEY`；不要复制个人 HOME、Docker socket 或
control secret。Git/gh 每轮只使用 GitHub App 下发的 assignment 凭据和临时配置，不改个人全局
Git/gh 设置，提交作者仍由 worker Git 配置决定。Amp 历史读取是完整原生 thread 快照，可能滞后；
未知续跑不会自动重跑，Amp adapter 固定使用 `--mode low`。INITIAL 会在
`<worker_root>/repos/<owner>/<repo>` 缺失或为空时，用当前任务的短期 installation token
原子 clone；已有 seed 保持原状，不自动 fetch/reset 或安装依赖。seed 必须有任务基线分支，
不自动处理空远端、submodule 或 LFS。

每个 worker 按仓库维护执行位置池。controller 按排队需求请求补充，worker 先创建并登记真实
目录，调度再直接选择 `(worker_id, workspace)`。`FLOWTOOL_WORKER_REPO_WORKSPACE_LIMIT=2`
设置每仓库上限（正整数、最小 1，重启生效）；设为 3 可同时绑定三个 Flow。不同仓库独立计数，
seed 和默认推理 workspace 不计入。每个 Flow 独占一个 workspace，同一 worker 可在不同目录
并行执行多个 job；review 保留原绑定，同一 Flow 的 job 串行。容量不足时新 Flow 排队。
归档后完整副本保留 ignored 缓存，临时 worktree 注销删除；清理及业务 ACK 结清后才可复用。
目录异常或所有权不明时保留证据，不自动清掉未知文件或接管未登记目录。数量上限不限制缓存字节数。

每次 INITIAL、REVISION resume/fallback 都提供本轮 bot `gh` 包装、独立配置和 Git helper，
不使用个人登录态；token 过期/权限不足即失败，不自动续期或扩权。worker 保持单仓库
Contents write，PR 通过 MCP `open_pr` 请求 controler 校验后创建。包装不构成 OS 沙箱。
当前 Codex/Amp 接入均使用 wire21 / journal5；执行结果使用 ResultInbox v1，Control 本地归档通知使用 v2。
serve 与 worker 的协议版本必须匹配。
通信统一为顶层控制和 typed call/result 列表；控制 ACK 仅确认接收，不表示业务提交。
支持只读加载受限 v4 journal（内嵌 v14/v15 回执），正常保存时才升级，见 [Worker 更新](docs/deployment/worker-update.md#journal-直接前代兼容)。
两个后端继承 worker 专用账号及任务仓库中原生 CLI 可发现的 skills/MCP，以及其自定义环境变量；
不复制当前对话或其他机器的配置。Amp 保留用户/workspace 设置与原生审批，仅覆盖保留名称 `flow`，
首次执行使用当前 broker，修订时阻断旧 broker。control/worker、Git/GitHub 认证及父 agent 身份
仍先过滤，再注入本轮能力。自定义秘密不保证全面脱敏；配置和隔离边界见[部署说明](docs/deployment/README.md#skills-与-mcp-继承)。

推理使用 `worker_root/inference/<workspace>/answer-*` 下的新目录和新会话，不关联 GitHub、不 resume 编码会话，
也不授予 Flow MCP 或 GitHub capability。普通 worker 同时提供 execute/infer，无需绑定任何 repo
即可接推理 Job。每个 worker 有一个独立推理槽，可以与编码同时运行；推理取消不停止编码。
controller 托管的 bub mini worker 仅提供 infer，启动时创建无仓库默认目录，每次请求使用其独立
临时子目录；重连保留父目录，退出时清除，不创建仓库或编码 journal。
worker root 位于 Git 仓库、worktree 或其符号链接内时不提供推理能力。仅分开 cwd 不构成沙箱：
原生工具、用户配置和自定义凭据仍受下述专用账号/container/VM 隔离前提约束。

`WorkerSettings` 不读取当前目录的 `.env`，worker 配置通过进程环境提供；默认要求 `wss://`，示例中的
`FLOWTOOL_ALLOW_INSECURE_WORKER_WS=true` 仅用于开发。要运行 `danger-full-access` CodingAgent，必须在专用 OS account、
container 或 VM 中只注入 worker/Codex 所需变量，并设置
`FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 与对应的
`FLOWTOOL_EXECUTION_ISOLATION=dedicated-account|container|vm`。开启该模式时，worker 进程环境若包含数据库、
GitHub App、webhook 或 control worker-registry secret，会拒绝启动。仅使用不同 repo 目录不构成这个 secret 边界；
同一 VM 可以运行多个隔离 worker，但 control plane 的凭据和 `.env` 必须对其 OS 身份不可读。

Compose 中的 PostgreSQL 账号只用于本地开发。远程部署必须使用独立密码和 TLS；serve 会初始化
schema，因此数据库账号需要拥有应用 schema，但不应具有数据库外权限。项目不自动导入旧 SQLite 文件。
也不要用示例 Webhook secret、worker token 或 allowlist 启动可公开访问的服务。启动 serve
和 `/healthz` 不等于 GitHub/Codex/bub 集成已经可用；接入外部服务前请先阅读
[GitHub App 接入说明](docs/architecture/github-app.md)和[已知短板](docs/architecture/decisions.md)。多个 worker
必须各自配置唯一 identity/token；未显式设置 root 时会按 worker id 派生，显式 root 由 identity 标记和进程锁防止复用。
worker 是长期进程或容器，而非每个 Flow 的临时容器；取消不表示容器会自动回收。
serve 的 heartbeat、lease、recovery grace 配置见 [`env.example`](env.example)；worker 的
`FLOWTOOL_RECONNECT_MIN_BACKOFF`、`FLOWTOOL_RECONNECT_MAX_BACKOFF` 和 `FLOWTOOL_RECONNECT_JITTER`
通过进程环境调整。非法时间关系会在启动时拒绝。

常用运维入口：

```bash
uv run flowtool --help
uv run flowtool flows --active
uv run flowtool workers
uv run flowtool flow show <FLOW_ID>
uv run flowtool flow cancel <FLOW_ID>
uv run flowtool flow retry <FLOW_ID>
uv run flowtool flow finish <FLOW_ID> succeeded --summary '调查结论' --expected-round 1
uv run flowtool flow resume <FLOW_ID> --expected-round 1
uv run flowtool flow history <FLOW_ID> --round 1
```

运维命令在可访问数据库的 control 环境运行。`flow show` 会列出未完成 GitHub 操作的状态、
下一次尝试时间和错误原因；`flow retry` 重排最近失败的任务。已核验并 push 的修订若遇到 GitHub
明确拒绝通知（例如 reviewer 请求 422），结果和回执保持待完成；修正 GitHub 端原因后使用同一命令
只重试失败通知，成功后原子提交交付/session/反馈，不重新运行修订。状态不明的操作仍须先核对。

直接输入也可用管理命令 `flowtool flow create OWNER/REPO --installation-id N --content-file PATH`；
`--content-file -` 读取 stdin，也可用 `--content TEXT`。Issue 输入改用 `--issue-number N`，不得同时提供内容。
这两种输入共用 `POST /api/flows` 的校验和原子创建用例；API 要求 owner 的同源 JSON，成功返回 201。
`GET /api/flow-targets` 提供授权目标的分页清单，`/api/repos` 仍只用于历史看板筛选。

serve 后台补齐三个配置标签：`FLOWTOOL_TRIGGER_LABEL`（默认 `flow`）、
`FLOWTOOL_IN_REVIEW_LABEL`（默认 `flow:in-review`）、`FLOWTOOL_DONE_LABEL`（默认 `flow:done`）。
名称不得为空或重名（忽略大小写）。Projects 和看板仍映射 Flow.state，finalizing 映射进行中。
未建齐或检查失败的仓库每 **300 秒**检查；确认标签齐全后每 **24 小时**复查并补回缺失项，
人工预建的也计入，已有名称、颜色和描述不变。检查时间只保存在内存，重启后重新扫描确认。
新安装与新增授权仓库每 300 秒发现；暂停的 installation 和归档仓库跳过。
App 需有 Issues write 权限；初始化失败不阻塞服务 readiness，日志记录待重试原因，限流可延长等待。
标签定义初始化不向 Issue/PR 添加标签。首次交付及满足重新送审条件的修订为 PR 添加待审标签；
合并清理确认后为 Issue/PR 添加完成标签、移除待审标签，增量操作保留用户和触发标签。
这些展示操作沿用 `LabelEffect`，GitHub 读取或写入失败时保存错误并标记 skipped，不再等待或重试该操作，
也不阻塞 PR 关联、交付、停止确认或归档。响应不明时保留发送记录，不伪造成功回执。
不持续同步六态、不保证标签最终一致，也不自动修复结果完成后被人工改动的标签；trigger 标签的触发职责不变。
`flow show` 区分清理与 GitHub 回写进度；清理已确认但评论等必要回写被明确拒绝时，修正远端原因后
执行 `flow retry <id>`，只重置原 job 的失败 effects，保留已完成回执、attempt 和 worker journal。
评论发送前的本地认证失败也可在修复凭据后按此方式重试；后续查询失败不会清除已有不确定发送记录。
状态写入由持久恢复逻辑处理，避免 SDK 内部重试掩盖此前已发送的请求；读取（含只读 GraphQL）
与 push token 签发保留有限重试。必要操作的确认查询失败仍等待恢复，并遵守 Retry-After 或限流重置时间。
必要操作的 429/5xx 由恢复循环继续处理；评论结果不明时先查 marker，不盲目补发。收尾期间 Flow 保持 finalizing，
永久失败进入 blocked，原 worker 仍被保留。配置变更不改写已经冻结的通知。
`on_archived` 是提交后的尽力插件回调，异常不破坏完成，不承诺跨崩溃仅调用一次。

轮次只保留在 Flow、Job、Attempt 和 activity 字段中，不再单独保存 Round 实体。
已核验 webhook 与补偿查询共用自动更正规则：只有实际合并时间加一秒仍早于原归档时间，
才将 abandoned 的有效结果改为 succeeded。原始归档、合并证据与更正分别作为不可变 activity 保存；
没有人工更正入口，不改变原归档时间、新轮次、Issue owner 或执行绑定。归档后才合并或顺序不明时只记事实。
旧四态库启动会拒绝旧状态：先停止消费者与派发、备份数据库，再用 `scripts/migrate_flow_states.py`
预检与显式迁移；存在未知执行、旧未完成计划或归属冲突时必须先核验，不能强制猜测归档。
其中已应用且通过格式、身份与摘要校验的 v13 结果（不含 inference），会列入预检的 `inbox_conversions`，
备份原行后仅把 `result_payload.protocol_version` 改为 14，正文和 digest 不变，与六态转换同事务提交。
v14/v15 和 ResultInbox v1 不改；未应用或损坏的结果仍拒绝，不转换 worker journal 或在线协议。
应用需 `--apply --expect-report <预检指纹>`；数据、schema 或迁移规则变化后须重新预检，旧脚本指纹不能复用。

## Web 控制台

控制台在 serve 的 `/console/` 提供，看板、详情、执行记录和时间线通过同源 `/api/` 读取。
页面、静态资源及 API 都要求现有 Cloudflare Access owner 身份；缺少配置时拒绝访问。
Tunnel/Access 必须将 `/console*` 与 `/api/*` 交给同一服务并应用相同 owner 策略，
不要为静态资源增加公开旁路。Webhook 与 Worker 端点继续使用各自原有鉴权。

时间线永久保存已观察版本和删除标记；本次阅读绑定本地快照，末尾分界线显式刷新。
活动轮次按实际 Job/Attempt 或 PR 身份确定，迟到内容不借用当前轮；Issue 共享内容及无法确认归属的旧内容为 NULL。
活跃 Flow 打开详情或刷新会排队后台同步，未完成的来源明确显示 pending/partial/unavailable，
不将尚未同步或权限错误当成“没有更新”。Worker 会话复用主动分页查询，心跳不触发全量抓取。
首次采集前的历史编辑/删除、远端已过期内容无法还原；个人阅读状态另见 #88。

从源码制作带控制台的 wheel/sdist 时，先在 Node 24 环境构建静态文件：

```bash
python scripts/build_console.py
uv build --no-sources
```

生成的静态文件随 Python 包安装，serve 运行时不需要 Node；Docker 使用独立 Node 构建阶段。
仅运行 Python 源码而未构建控制台时，`/console/` 返回明确的资源不可用错误，不退回示例数据。
前端开发可在 `app/controler/web` 运行 `npm ci && npm run dev`，API 代理至本地 serve 的 8000 端口。

## 验证基线

本地检查：

```bash
uv run pytest -q -p no:cacheprovider
uv run ruff check app tests
uv run mypy app tests
python scripts/build_console.py
uv build --no-sources
uv run flowtool --help
```

未设置 `FLOWTOOL_TEST_DATABASE_URL` 时，PostgreSQL 集成用例会跳过。启用开发数据库后运行完整测试：

```bash
docker compose up -d postgres
FLOWTOOL_TEST_DATABASE_URL=postgresql://flowtool:flowtool@127.0.0.1:5432/flowtool uv run pytest
```

[GitHub Actions CI](.github/workflows/ci.yml) 会在 pull request、推送到 `main` 和手动触发时，使用
Python 3.12 分别运行 Ruff、Mypy、pytest 与 sdist/wheel 构建。pytest job 配置 PostgreSQL 17，
并启动 serve 和两个 worker，重启 serve 后验证 worker 自动重新注册。
自动触发的 pytest 使用 `pytest-testmon`，根据缓存的运行时依赖记录只执行受代码变更影响的测试；
首次运行、缓存失效，以及依赖、共享 fixture、测试数据、脚本或部署配置变更时执行全量。
手动触发 CI 也会执行全量并更新依赖记录。本地 `uv run pytest` 仍默认全量。
包边界检查和安装后的 Worker CLI 测试涉及源码读取或隔离的代码副本，始终执行，避免运行时依赖追踪漏检。

自动化覆盖配置与协议、Webhook、任务调度和恢复、Flow 工具与 PR 交付、GitHub 超时及错误映射、
结果重放与远端操作核对。数据库用例还覆盖事务回滚、跨连接并发、任务去重、取消交错和恢复期限；
GitHub、Codex 与模型服务使用替身；Judge 契约测试运行真实 bub 库连接本机假模型端点，不访问真实 provider。

2026-09-07 M2 F4 本机复核：完整 pytest **377 passed / 0 skipped**（真实 PostgreSQL），Ruff、Mypy 和发行包构建通过；
覆盖反馈批次、Judge/答案恢复、Git/jj 修订核验、session 兜底，以及线程分页、原位回复、保守 resolve、
两轮反馈处理和 PostgreSQL 故障恢复。保留一个
Starlette TestClient/httpx 弃用告警。数据库 CI 的历史验证见
[M1 交付修复记录](docs/plan/m1-delivery-review-fixes.md#实施结果)，原子结果改动的验证边界见
[实施记录](docs/plan/atomic-workflow-results.md#实施记录2026-09-06)。

## 升级注意

M3 F2 本地验证（2026-09-08）：真实 PostgreSQL 下 **461 passed / 0 skipped**，含真实 Git/jj
目录及 worker/control 恢复测试；Ruff、Mypy、sdist/wheel 构建通过。仅保留既有 Starlette 弃用告警。

M3 F3 及审查修复本地验证（2026-09-08）：真实 PostgreSQL 下 **502 passed / 0 skipped**；包含归档状态回写、
响应丢失与重启恢复。Ruff、Mypy 与 sdist/wheel 构建通过；真实 GitHub App/worker 验收仍待执行，
详见 [F3 审查修复记录](docs/plan/m3-archive-status.md#7-审查修复2026-09-08)。

当前在线协议仅支持 **v21**，正常写入 worker journal **v5**、执行结果 ResultInbox **v1**、workspace manifest **v2**；
Control 本地归档通知使用 ResultInbox **v2**。
INITIAL/REVISION 使用字符串 `task_content`，Issue 在准备时由最新标题与正文拼接；server/worker 必须成组升级，不能混跑 v20。
现有六态库启动会幂等新增 `flows.task_content`（TEXT）和可空的 `title` / `title_job_id`、允许 NULL Issue，并校验两种输入恰好存在一种。
首次创建直接任务后，旧程序无法读取这些记录，即使任务已归档也不能直接降级；应前向修复或按备份整体恢复。
`workers` 使用 `(worker_id, workspace)` 复合主键；旧单行 worker schema 启动时明确拒绝，
须在停写、备份后核对真实 worker-root 身份并显式迁移，不自动生成或猜造 workspace。
合法 v4 journal 的 v14/v15 回执与已知旧 inbox 只读转换；下次原本需要的状态写入才写 v5。
保留任务身份、原摘要、停止证据与工作区归属；未知、混合或损坏记录拒绝并保留，不自动清理。
运行时不支持 v3 journal / 协议 v13，也不支持任意旧数据库转换；新旧 wire 不在线互通。
四态离线迁移仅为已校验、已应用的 v13 数据库结果提供 v14 转换，不扩大 journal 或在线协议兼容范围。
该迁移只转换 Flow 状态，保留旧执行表；完成后仍需单独解决 workspace schema 迁移。
首次运行使用新数据库/schema 和新 worker root；受支持的升级保留原 root。
本次协议切换需要协调 serve/worker 的维护窗口：停止新分配、等待活跃执行和外部 effects 收敛，
备份并核对实际持久版本后再升级。写入新格式后不能直接降级旧二进制，详见[升级边界](docs/operations/upgrade.md)。
Worker 连接按实际 protocol 和认证结果处理，不匹配直接拒绝。支持的 uv Git 虚拟环境安装可通过 `flowtool worker --update`
原地更新本体及必要依赖，核验后退出；操作者须先停止 Worker，再按原方式启动，详见
[Worker 更新](docs/deployment/worker-update.md)。源码与容器继续按原部署方式更新。
Worker 没有后台自动更新或切换握手；同版本重启仍保留冻结任务、结果摘要、effect ledger 与 journal 恢复能力。

每个 Flow 的 journal 独立存于 `journals/flow-<flow_id>.json`，runtime 扫描全部文件，不再使用
`current.json` 路由。ACK 只确认 journal，不写 pool，也不阻止已退出执行的位置被新代接替。不要更换
`FLOWTOOL_WORKER_ROOT`、复制 journal 到其他 root 或复用 worker ID 接管工作区。新 INITIAL
取得独占后清理池中登记且归属匹配的 `workspaces/<owner>/<repo>/<slot_id>`，保留可复用目录及
ignored 缓存；等待 review 的目录保留给原 Flow 的 revision，不能按空闲目录处理。
拒绝 symlink、路径逃逸和未登记目录，不删除 seed 或其他 Flow 的工作区。

合并后使用 `flow show <id>` 分别查看业务归档、执行绑定与 GitHub 通知状态。原 Worker 确认执行
退出后可释放绑定；下一 INITIAL 在独占目录后清理旧副本，清理失败不把原已归档 Flow 改回 blocked。
已归档 Flow 的
`flow retry <id>` 只重试明确失败的冻结通知，不能重启编码或重新派发清理；未决远端结果只查询恢复。
合并统一使用原 Worker 的停止协议回收，不派发 Worker ARCHIVE，也不新增旧 ARCHIVE 任务恢复。
旧 executor 状态未知、journal/root 丢失等情况仍隔离目录并要求人工核验，不跨 worker 接管。
升级及回退边界见[部署与重启](docs/operations/upgrade.md#合并归档与资源回收分离)。

线程快照、处理依据和远端回执均按当前契约持久化；本地 resolved 标记不替代 GitHub 确认。
线程权限失败可完成已验证代码交付，但相关反馈/诊断仍待处理。

修订失败保留原 worker binding；处理脏工作区或远端偏移后可显式 retry，不自动 reset/rebase/force push。
修订准备阶段的 GitHub 错误会持久记录为失败；恢复后显式 retry，未 Started 的准备失败不消耗修订轮数。
INITIAL、REVISION resume/fallback 的每次编码调用由 adapter 管理空闲期限，没有编码总时限。
历史导出仍有 20 秒总时限，单条 VCS 命令仍有 30 秒总时限，超时回收并等待对应进程组退出；
用户 cancel 通过持久停止握手取消执行，未确认停止或远端结果不明时继续保留绑定。

可选组织 Projects v2 的配置及只读 `flowtool project check <repo>` 见 [GitHub App 接入](docs/architecture/github-app.md#a4-可选的组织-projects-v2)。

Projects 自动同步已接入；`flowtool project status <repo>` 查看状态，`flowtool project retry <repo>` 在修复远端条件后重新核对。
