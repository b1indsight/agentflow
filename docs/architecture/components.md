# 组件职责

本页同时写目标职责和当前边界。组件的汇总状态以[架构入口](README.md#当前实现状态)为准。

## 4.1 Webhook Ingress（FastAPI + uvicorn）

职责：读取原始请求体，用 Webhook secret 校验 `X-Hub-Signature-256`，把有效事件交给 router，避免
Codex 等长任务占用 GitHub 的 Webhook 请求。

当前实现：

- `GET /healthz` 返回 `{"status": "ok"}`；它是存活探针，不检查数据库或外部依赖；
- `POST /webhook` 缺少或带错误签名时返回 401；签名正确时创建后台 handler 并返回 202；
- `WS /internal/workers/connect` 按 token map 鉴权，并为每个 worker identity 保持一条连接；
- FastAPI lifespan 初始化 PostgreSQL schema，启动 reconciler、lease sweeper 和 result inbox recovery，不启动进程内 worker；
- 自动化测试覆盖 health、错误签名、正确签名、202 交接和一条 signed label→job 路径。

边界：202 表示事件已交给进程内后台 task，不表示业务处理或持久化已经成功。进程在 handler 落库前
退出仍可能丢失事件，未来由可靠入站/对账语义补强。

## 4.2 Event Router

当前实现：

- 忽略配置 bot login 及 GitHub `type=Bot` 的事件；
- 以 `X-GitHub-Delivery` 原子占有并持久化去重，只有成功插入的 handler 才继续处理，同时记录原始 payload；
- 仅允许 allowlist 用户通过 `issues.labeled(flow)` 创建 active flow 与 initial job；
- `issues.unlabeled(flow)` 或严格等于 `/flow stop` 的评论取消 active flow；
- review body、inline comment 和 conversation 按 Flow/kind/GitHub ID 保存不可变版本，保留来源 attempt；
- 反馈 delivery、版本和 deadline 共用事务，持久 settle 后冻结批次；allowlist 强信号经 grace 直达 REVISION；
- `pull_request.closed (merged=true)` 驱动 MERGED；approve 本身不触发修订。

M2 F1 自动化覆盖反馈工作流锁、同 Flow 批次串行、重启恢复和精确版本消费。尚未实现：
未合并 PR close 的取消语义、后台 handler 失败后的自动 delivery 重试，以及真实 GitHub payload
的完整兼容性。Reconciler 仍是桩，不能作为当前补偿路径。

## 4.3 Control Service / Job Coordinator（状态机核心）

目标状态机：

```text
PENDING → CODING → PR_OPEN ⇄ REVISING
                      │
              人工 Approve + Merge
                      ▼
                    MERGED → ARCHIVED

任意活动态 → CANCELLED
运行失败   → FAILED
```

`ControlService` 在显式事务中负责触发、取消、合并和 retry；`JobCoordinator` 把五类持久 job 构造成版本化 task，
并把已接收的结构化 result 交给 `ResultApplication`。后者冻结一次结果的领域变更和 GitHub 操作，
由 `ResultEffects` 查询/执行远端操作并保存回执，最后一起提交本地状态、派生任务、运行记录和结果确认。
网络请求不占用数据库事务；plugin hook 在提交后调用，不具备持久化投递保证。worker 不能直接更新这些状态。
非零 Codex exit 会转 FAILED。revision 成功后仅消费明确 addressed 的 item/version，deferred/遗漏项/
clarification 保持 waiting；不把 addressed 当作远端 resolved。有未决问题时不重新请求审核。

当前 coordinator 仍有以下缺口：

- review comment 到 GraphQL thread 的映射尚未接入，无法真实 resolve review thread；
- `_pr_context()` 只有 repo、PR 和 issue 编号，没有 diff、CI 或未解决线程；
- cancel 只更新数据库状态，不 kill Codex、不清工作区；
- 未合并 close、人工 push、rebase/冲突尚未实现。

因此状态转换代码存在不等于目标闭环可用。

### 4.3.1 远程 Worker Gateway / Client / Executor

serve 的 `WorkerGateway` 校验每个 identity 的 bearer token，维护多会话 registry，并用随机 connection epoch 与
assignment token 对 Started、Heartbeat 和结果做四元组 fencing。每个 worker 是单槽，PostgreSQL 按 FIFO、
worker `last_assigned_at` 和 Flow execution binding 事务化领取任务。worker 的 `WorkerClient` 主动连接并指数退避；
`WorkerExecutor` 只依赖 CodeAgent、Judge 和 Flow repo/VCS helpers，不导入 Store、FastAPI 或 GitHub。

不同 identity 可并发在线；同 identity 新连接取得新 epoch 并关闭旧 socket，数据库条件保证旧连接最终失效。
任务未 Started 断线后保留到 lease 到期再 requeue；Started job 进入 RECOVERING，只允许原 identity 在 grace 内恢复，
到期才把 job/活跃 flow 置 FAILED，不覆盖 CANCELLED。取消只退掉 queued job，running executor 仍需交回结果或等待租约清理。
已持久接收的终态结果由 control 接管，不因 worker 的 recovery deadline 到期重新执行或失败；
后台循环持续恢复未应用结果。必要的 GitHub 操作未完成时不发 ResultAccepted，worker 保留 journal 重连。
Flow 非终态时，即使暂时没有 running job，binding 也继续独占该 worker/repo。
worker socket 与 executor 解耦，root 内原子 journal 在 ack 前保存进度和终态结果；
进程重启可重放终态，但不会猜测恢复已经消失的 executor。同一 attempt 的后续任务仍只回到首次 Started 的 worker，
不支持跨 worker session 迁移。同机默认 root 按 identity 派生，并用持久 identity 标记与独占锁拒绝并发或顺序复用。

## 4.4 Codex Driver

`CodexAgent` 已实现 `CodeAgent` 端口的 `start` / `resume`：

- start 仅在显式 `FLOWTOOL_TRUSTED_HOST_EXECUTION=true` 时使用
  `codex exec <prompt> --json -C <flow-repo> -s danger-full-access`；
- start/resume 都要求 trusted-host 开关；worker 还必须声明专用 account/container/VM 隔离，且不得继承
  control-plane 环境变量；
- 从 JSONL 的 `thread.started.thread_id` 读取 session id；
- resume 使用 `codex exec resume <session_id>`，cwd 与 sandbox 继承自会话；
- `-o` 读取最终消息，INITIAL 和 revision 通过 `--output-schema` 解析结构化结果；
- 子进程只继承 Codex 认证、代理/CA 和基础运行环境；INITIAL 额外获得单次短时 push credential helper 与
  指向 0600 Unix socket 的 `flow.open_pr` STDIO MCP 配置，不获得 control/worker credential；
- 单次运行使用配置的 timeout，超时 kill 子进程并返回 exit 124。

2026-08-01 的独立 CLI 实测证明 `exec` / `resume` 上下文连续、workspace-write 可修改工作区、jj
`.jj` 可写且能 commit。它没有证明当前 driver 能在真实 flow 中完成 issue→commit→push→PR。

当前 worker 返回 exit/result、worker 验证的 VCS facts 和 `flow.open_pr` handle；coordinator 把 kind 和 exit code
写入 `codex_runs` 并与持久 tool outcome 核对。跨协议不回传完整 prompt 或原始 JSONL，因此
`prompt`/`log_path` 为空。GitHub-only egress 和多轮 context 增长仍待验证。

## 4.5 GitHub Client

目标职责：以 GitHub App installation 身份创建 PR、评论、设置 labels、请求 reviewers、列出 review
threads、resolve thread，并签发 push 所需的短时 installation token。自动合并明确不在职责内。

`GitHubApp` 已对齐锁定的 githubkit 0.16：返回稳定的 Issue、repository、remote ref 和 PR snapshot，
用 App 身份签发只含单仓库 Contents write 的短时 capability，并把常见 HTTP/认证故障映射为不含响应
body 的类型化错误。真实 GitHub App smoke 尚未执行；review thread GraphQL、Project v2、关闭 issue 和
reconcile 仍未校准。接入边界见 [GitHub App 文档](github-app.md)。

## 4.6 本地平面：Flow repo / execution policy / VCS

| 轴 | 是否端口 | 当前载体 | 当前边界 |
|---|---|---|---|
| Flow repo | 否 | `prepare_flow_repo/cleanup_flow_repo` | stable flow id 派生独占完整 repo；同 Flow 复用、不同 Flow 分离 |
| execution policy | 否 | worker config / `CodexAgent` | full access 需专用 account/container/VM；目录只表达所有权 |
| VCS | 否 | `agent_guidance/verify_pushed_outcome` | Agent 自探测；worker 在 tool 前用固定 remote 和净化、限时命令验证 ancestry/clean/remote SHA |

分支名由 control 定为 `flow/<flow-id>`。worker 内的 Code Agent 在 Flow repo 完成 branch/bookmark、commit 和
push；worker 在转发 `flow.open_pr` 前核验，control 再读取 GitHub ref 核对 SHA 后才创建/复用 PR，最后以
terminal result 三方核对。当前缺口包括：

- 每 Flow 容器/微 VM 和互不信任租户的强隔离仍属 M5；M1 至少要求 worker 与 control secret 分离；
- 仅允许 GitHub egress 的策略未实现；
- installation token 尚未在真实 GitHub 上验证，且 GitHub-only egress 未强制；
- 大 repo 的完整复制成本和 terminal cleanup 故障恢复仍待验证。

## 4.7 Store（PostgreSQL）

当前 Store 使用 Psycopg 3 的同步连接池访问 PostgreSQL：

`Flow`、`FlowExecution`、`FlowDelivery`、`FlowToolCall`、`Attempt`、`Job` 和 `ReviewItem` 是 Pydantic
持久化模型。返回这些领域对象的查询在
cursor 上使用 Psycopg `class_row(Model)`，由模型统一校验枚举、时间和其他字段；其余轻量查询继续
使用 pool 的 `dict_row` 默认值。SQL、事务边界、JSONB 适配和并发协议仍由 Store 显式维护，没有引入
ORM。

- `flows`：issue 级状态，`(repo, issue)` 仅允许一个非终态 flow；
- `flow_executions`：Flow 到单槽 worker/repo identity 的生命周期 binding 与 recovery deadline；
- `flow_deliveries`：每 Flow 唯一 branch/base/PR、交付状态和版本；
- `flow_tool_calls`：fenced command 的参数与终态 response/error，支持同 call id 重放；
- `attempts`：一次运行/恢复尝试的 session 与 revision cycles，不拥有 worker、branch 或 PR；
- `jobs`：queued/running/done/failed、worker/token/start、lease/recovery deadline、result inbox/digest 与尝试次数；
- `result_applications`：按源 job 唯一保存冻结的结果计划；
- `result_effects`：有序 GitHub 操作、claim/fencing、远端回执、退避时间和最后错误；
- `workers`：identity、协议版本、epoch、online/busy/recovering/offline/failed、当前 job、连接/心跳与断线原因；
- `events`：delivery 去重和 payload 审计；
- `codex_runs`：按 attempt 记录 prompt、kind、exit 与时间；
- `review_threads`：旧反馈历史，升级时一次性导入最新对象；旧 resolved 标记不作为 GitHub 确认；
- `review_items` / `review_versions`：Flow 内稳定反馈身份、不可变版本及独立 relay/consume 时间；
- `review_pending_edits`：同秒乱序编辑的前序正文与原始来源，供前序到达后原子回放；
- `review_buffers` / `review_batches`：持久 deadline/generation、冻结快照、批次所有者 job 和未决问题。

自动化覆盖 active 唯一约束、flow/attempt/job 往返、review upsert、assignment start/heartbeat/fencing、
lease 到期 requeue、started grace/deadline、epoch fencing、result inbox 去重、跨 Store 可见性、delivery 去重和并发 assign。
`assign_job()` 使用事务内的 `FOR UPDATE SKIP LOCKED`，gateway 接受多个独立配置 worker。

Store 继续使用同步接口。assignment claim 会锁定公平候选空闲 worker 和最早 eligible Flow job，并原子创建
或复用 lifecycle binding。`with store.transaction() as tx` 提供连接绑定的 Store 视图，各领域步骤显式使用 `tx`。
结果的最终事务同时写入 flow/delivery/attempt、带源 job 唯一键的派生 job/codex run、`result_applied_at` 和 worker slot。
`flow_tool_calls` 的 prepared delivery 与 response 也一起提交。

评论使用稳定隐藏 marker 并分页核对本 bot 作者，PR 使用固定 head 查询，labels/reviewers 则核对目标状态。
不能确认是否成功的非幂等请求保持 uncertain，不盲目重复 POST；`flow show` 显示待处理操作，retry 不能绕过它们。
因此不宣称跨 PostgreSQL/GitHub 的通用 exactly-once。跨 worker session 迁移和多 serve HA 仍未实现。
升级前应停止旧 serve；新增字段/表采用 additive schema。旧版本已接收却未确认的结果可能有未记账副作用，
默认阻止自动重放并要求人工核对；不要在未核对远端前手工清除结果标记或操作账本。
M0 的 SQLite 文件
不会自动导入 PostgreSQL。

## 4.8 GitHub 交互面

目标用户体验完全位于 GitHub：标签触发、PR review 反馈、网页人工合并、状态 labels 与评论。

当前 router 已识别触发、取消、review/comment 和 merged 事件；PR 创建、评论、reviewer 和 label 已有
经隔离测试的 adapter primitive，且 reviewer 配置不复用 trigger allowlist。真实 Issue/default branch、
head PR 查询和 capability 已接入 INITIAL；thread resolve 与 Project 状态仍未校准。
工具没有自动合并实现。

## 4.9 Context Builder

当前 `ContextBuilder` 是纯字符串组件：

- initial prompt 包含 issue 标题/正文、首个 `AGENTS.md` 或 `CLAUDE.md`、VCS 指引和硬约束；
- revision prompt 包含 Judge instruction、冻结批次的 item/version、inline path/diff hunk/original commit id、
  VCS 指引和 `CodexResult` 输出约定。

INITIAL 已使用 GitHub Issue 的真实标题与正文；revision prompt 没有当前 diff、CI 状态或完整未解决线程清单。
issue/PR 文本是不可信输入，后续接入必须保留明确的数据/指令边界。

## 4.10 Judge / Triage

目标语义见 [Review 修订循环](review-loop.md)。当前 `BubJudge` 通过 subprocess 执行
`bub run <prompt>`，要求返回 JSON，再校验为 `TriageVerdict`；解析失败保守返回 `wait`。`answer()`
使用另一条简短 prompt。

实际 bub 命令、模型配置、退出码处理、结构化输出稳定性、置信度行为和真实 PR 讨论效果均未校准，
因此当前状态是桩，而不是已验证 adapter。
