# GitHub App 接入

> 状态：已对齐 githubkit 0.16，并接入 M1 Issue→PR 与 M2 线程回复/resolve 自动化路径；尚未在真实 App 上执行读写 smoke。

GitHub App 提供独立 bot 身份、Webhook 和短期 installation token。FlowTool 不需要代表某个用户执行
操作，因此当前设计不使用 OAuth user token。

## A.1 认证链

```text
私钥 + App ID / Client ID
   │  RS256 JWT（exp 最多 10 分钟）
   ▼
App JWT ── POST /app/installations/{id}/access_tokens ──► Installation token（1 小时）
                                                           │
                                                           ├─ REST / GraphQL
                                                           └─ HTTP Git（目标：push）
```

- App JWT 只用于以 App 身份调用相应 API、获取 installation token；
- installation token 的权限和仓库范围不能超出 App installation 已获授权的范围，创建 token 时还可进一步
  收窄；token 一小时后过期；
- GitHub App Webhook payload 包含 `installation.id`，当前 Store 会把它保存到 flow；
- HTTP Git 可把 installation token 作为密码，用户名使用 `x-access-token`；需要 Contents 权限。

官方依据：

- [生成 GitHub App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [以 GitHub App installation 认证](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
- [选择 GitHub App 权限](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)

## A.2 注册与最小权限

在个人账号或组织下注册 App，配置 Webhook URL 和 secret，再按实际使用的 endpoint 申请最小权限。
当前目标能力需要评估：

| 能力 | 候选 repository permission | 用途 |
|---|---|---|
| Git 内容与 HTTP push | Contents: read/write | 读取代码并推送分支 |
| issue 评论与 labels | Issues: read/write | 状态评论、触发/取消标签 |
| PR、reviewer、review threads | Pull requests: read/write | 创建 PR、请求审核、读取/解决线程 |
| 仓库身份 | Metadata: read | GitHub App 默认需要的仓库元数据 |

`FLOWTOOL_ALLOWLIST` 只控制谁能触发 flow；`FLOWTOOL_REVIEWERS` 独立配置 PR reviewer，空列表表示不
请求 reviewer。`FLOWTOOL_IN_REVIEW_LABEL` 配置 review 状态 label。

`FLOWTOOL_GITHUB_REQUEST_TIMEOUT_SECONDS` 控制 serve 端 GitHub App 和 installation 客户端的
连接、读取、写入、连接池等待超时，默认均为 **30 秒**；只接受正有限数值，修改后重启 serve 生效。
这是 HTTPX 各网络阶段的超时，不是包含重试、分页和锁等待在内的整个结果恢复时限。
超时仍走现有 transient 错误处理；可能已发送的非幂等写入保留待核对，不能直接重发。

订阅事件候选：Issues、Issue comment、Pull request、Pull request review、Pull request review comment。
GitHub 会根据已选权限决定哪些事件可订阅；正式接入时应逐事件核对
[Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)。

安装 App 时只授权目标仓库。记录 App ID、私钥路径、Webhook secret；installation ID 从 Webhook
payload 获取，不需要静态写入 `.env`。

## A.3 已实现能力与缺口

归档前在目标仓库创建 `FLOWTOOL_DONE_LABEL`（默认 `flow:done`）和 `FLOWTOOL_IN_REVIEW_LABEL`
（默认 `flow:in-review`）。两个状态标签与 trigger label 不得重名（忽略大小写）或为空。
Issue/PR 标签和归档评论需要对应的 Issues/Pull requests write 权限；状态回写使用
[Labels API](https://docs.github.com/en/rest/issues/labels) 的增量 add/remove，保留用户标签和 `flow`。
缺少 label 定义或权限会保留清理凭据与未完成回写，不自动创建仓库 label；修正后通过
`flow retry <id>` 继续原结果。仅标签缺失且目标仍可读取时把 remove 404 视为已完成。

`GitHubApp` 启动时验证正 App ID 和可读非空私钥，使用 githubkit 0.16 原生认证。
control 按 Webhook 保存的 `installation_id` 绑定 `Installation`，再绑定具体仓库、Issue、PR 或线程；
看板配置绑定安装下的 Project/Item。资源查询返回新实例，状态更新只复制已确认字段；旧事实不原地刷新。
已有身份可本地构造资源，未知事实访问会失败。`model_dump(exclude_unset=True)` 区分尚未读取的字段
与已知 `None`，私有 SDK/认证及父绑定不进入 JSON。domain 容器、持久层和 worker 使用同一个资源模型，
`PullRequest.list_files()` 和 `list_review_comments()` 在资源内管理 REST 请求和分页，
按项异步产出 SDK 已有的文件与 inline 评论类型，保留 patch、diff_hunk 等来源字段。
文件缺失的 patch/previous_filename（SDK Unset）在上下文映射为 None。
审查上下文的普通评论和 review 正文直接使用现有 `RemoteFeedback`，每类只请求最多
21 条，保留前 20 条；第 21 条或后续 cursor 表示截断。review 使用实际更新时间，
晚于批次截止的编辑正文不进入上下文。已删除作者保留为 None，上下文显示 `[deleted]`，
持久反馈扫描继续跳过没有作者身份的条目。
control 直接遍历条目，仅负责审核配额、哨兵、映射、裁剪、过滤和冻结，保持原有字段与 digest。
Issue/PR 的 `fetch_comments(cursor, limit)`、PR 的 `fetch_reviews(cursor, limit)` 和
`fetch_threads(cursor, limit)` 请求 GraphQL，返回不可变 `Batch[T](items, next_cursor)`。
thread 批次直接携带各 thread 已反序列化的首批最多 20 条评论，其 `comments` 也是
`Batch[ReviewComment]`；评论续读使用该 thread 的 `fetch_comments(cursor, limit)`。
批量大小范围为 1–100，两层游标各自属于对应列表；访问容器不会隐式请求下一批。
持久轮询每次调度最多一个请求；`control.reconcile_feedback.ThreadScanState` 单独保存
外层位置及未读完 thread 的元数据和评论游标，观察结果与进度仍在同一事务提交。
完整线程读取器复用查询字段和解码，保留请求/字节预算、跨页去重及完整覆盖判断。
PR 响应由资源私有工厂直接转换为实例，保留完整读取与部分响应的字段差异。
资源方法用 `with translate_errors(endpoint)` 包住 SDK 请求和响应解析，保持原错误范围与取消传播；
客户端通过原生 `with_auth()` 复制配置，写入关闭副本的重试，后续读取仍保持原策略。
`GitHubError.definitely_rejected` 统一使用目标请求未发送或真实 HTTP 拒绝的证据，
后者沿用 4xx 排除 408 的判定。GraphQL 业务错误映射出的状态码仅用于诊断，
HTTP 200 的部分 mutation 保留不确定性；后续读取失败不能清除之前的写入记录。

资源、评论、批量容器、ref、凭证、合并事件、Project 绑定配置、操作错误和重试计时统一从 `app.github` 导入，例如
`from app.github import GitHubApp, Issue, PullRequest, Repository`。
`ReviewComment` 与 `ReviewThread` 同属线程模块；其他查询数据按实际操作归入对应资源或反馈模块。
`__all__` 明确公开类型和 `retry_delay`；字段声明、解码与扫描器由包内使用。
导入公开类型与标准 JSON 恢复不加载 SDK；SDK 在实际 App 或客户端构造时加载。
父资源使用模块导入创建子资源，反向构造类型校验和 SDK 导入只保留必要的延迟加载。

已由自动化覆盖：

- Issue、repository/default branch、ref、按 head 查找 PR、创建 PR、评论、labels 与 reviewers 的方法；
- 通过公开 installation access-token endpoint 显式传入单仓库和 `contents: write`，并校验返回 scope；
- authentication、permission、not found、conflict、validation、rate limit、transient 与异常响应分类；
- capability 的 token 在 repr 和普通 JSON 序列化中脱敏。
- capability 只在 assignment 临发时签发，通过 wire-only secret envelope 到 worker；Codex 的单次子进程以
  环境 credential helper 使用 token，参数、result 和 worker journal 不保存 token；
- control 的 `flow.open_pr` 在 GitHub ref SHA 与 worker 验证事实一致后才 find-or-open PR，并持久化调用结果供重放。

线程能力已由自动化覆盖：GraphQL reviewThreads/comments 独立分页、完整性与身份校验；
REST 根评论回复、分页精确 marker 恢复、resolve 后远端复读及 GraphQL 部分错误分类。
单项查询直接返回同一资源模型；所有评论页收集后核对持久根评论身份，远端冲突统一返回 GitHub 操作错误。
线程读取共享 50 请求 / 2 MB 数据预算，超限保留未完成状态。Pull requests: write 用于原线程回复和 resolve。
REST 写路径见 [Create a reply for a review comment](https://docs.github.com/en/rest/pulls/comments#create-a-reply-for-a-review-comment)；
线程字段见 [GitHub GraphQL pulls](https://docs.github.com/en/graphql/reference/pulls)。

尚未验证或实现：

- disposable repository 上的真实 App 读写 smoke；
- 限制网络到 GitHub，以及显式提前撤销 installation token；
- Project v2 与真实 reconcile smoke；按编号 PR 查询和漏失合并补偿已有自动化。

因此不能仅凭配置真实凭据就把当前 daemon 视为可用集成。

## A.4 Webhook 校验

GitHub 使用 Webhook secret 对**原始请求体**计算 HMAC-SHA256，并把以 `sha256=` 开头的十六进制摘要
放在 `X-Hub-Signature-256`。当前实现使用 `hmac.compare_digest` 做常量时间比较，这一 M0 路径已有
自动化覆盖。参见 GitHub 官方的
[Validating webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)。

本地开发可选用可信隧道把公网 Webhook URL 转发到 `http://localhost:8000/webhook`；隧道本身不属于
FlowTool，也不替代签名校验。

## A.5 密钥管理

- 私钥和 Webhook secret 只通过环境/本地 secret 文件提供，不入库、不进入 agent 工作区；
- `.gitignore` 已忽略 `.env` 与 `*.pem`，`env.example` 只含占位值；
- App 私钥不得传给 Code Agent；push 只使用短期、最小范围的 installation token；
- capability 不进入 Store、job/result/journal、prompt 或日志；Feature 3 只在 assignment 临近发送时创建，
  retry 时重新签发，并负责 credential helper 注入与清理。


## A.4 可选的组织 Projects v2

App 需要额外的**组织级 Projects read/write** 权限，且 installation 必须对应配置仓库所属组织。
每个仓库最多配置一个既有 Project；只管理原 Issue 卡片的既有单选 Status 字段。
个人 Project、PAT、创建看板/字段/选项不在本期范围。

`FLOWTOOL_PROJECTS` 接受 JSON（没有此配置时零 Projects 请求）：

```json
{"owner/repo":{"installation_id":42,"project_id":"PVT_xxx","status_field_id":"PVTSSF_xxx",
"options":{"todo":"option1","in_progress":"option2","in_review":"option3","blocked":"option4","done":"option5"}}}
```

字段和选项按查询所得 ID 绑定；允许多个逻辑状态指向同一个选项，展示名称更改不影响映射。
修改配置后重启 serve。用 `flowtool project check owner/repo` 只读校验真实 repo installation、
组织 Project 可写性、字段归属和全部选项；命令不创建测试卡片，也不访问数据库。

`Project.list_items_page` 返回 items 和不可见内容标记，调用方必须完成遍历才能确认不存在；
`Project.fetch_item` 按字段 ID 读取已知条目的 Status。`Project.add_issue` 与
`ProjectItem.set_status` 是两次独立 mutation，均禁止 SDK 自动重试。
HTTP 200 中的 GraphQL errors/partial data 是失败，异常仅显示分类与 endpoint，不打印 token。
F8 提供接口；F9 已接入独立持久的 Flow 状态同步与响应丢失核对。

依据：[Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)、
[组织 Projects 权限](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)、
[字段与 item schema](https://docs.github.com/en/graphql/reference/projects)。
真实组织 Project 尚未配置，真实 App 只读 smoke 未验证；自动化覆盖受控响应和真实 githubkit transport。


### Projects 同步运维（F9）

启用配置后，ACTIVE 或此后发生状态变化的 Flow 会被扫描；既有投影的 Done/Blocked 持续补偿。
一 Issue 对应一张卡，重复 Flow 优先由当前 ACTIVE、否则最新 Flow 驱动；不批量导入既有历史终态。
SUCCEEDED→Done，FAILED/CANCELLED/CLOSED 和明确人工阻塞→Blocked，MERGED 待归档→In Progress，
未绑定 INITIAL→Todo，有排队/执行任务→In Progress，OPEN 空闲→In Review。
临时网络退避不会把 Flow 直接改为 Blocked，Projects 故障也不会阻止归档 ACK 和 worker 释放。

用 `flowtool project status owner/repo` 查看最多 100 条（可用 `--limit` 调整），包含 item、
期望/已确认版本、下次运行时间及脱敏错误；修复权限、字段选项或远端条件后运行
`flowtool project retry owner/repo` 重新核对。它不创建新卡、不解除远端归档，也不清除已知 item。

同步独立每 5 秒调度，沿用 reconcile 的批次/并发/轮询间隔设置。每卡一条数据库 session 锁
跨越远端 I/O，不占用 Flow 行锁；429/暂时错误持久退避并保留 Retry-After。
重启后先读取远端值，写响应丢失时不会把旧响应当新版本确认。
首次 add 在发送前记录不确定标记；若重启后完整查询仍找不到该卡，自动流程会阻塞，需人工
核对远端事实。`retry` 本身不会把不确定 add 变成安全重发。被删除的已知卡片也不会自动重建。
人工修改受管 Status 会在下一轮被当前状态覆盖，其他字段不变；移除配置并重启后停止请求。
