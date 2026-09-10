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

`GitHubApp` 启动时验证正 App ID 和可读非空私钥，按 Webhook 保存的 `installation_id` 构造 githubkit
0.16 client。core 只接收类型化的 Issue、repository、remote ref、PR snapshot 和 secret-aware push
capability，不接触 githubkit schema。

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

adapter 逐页返回 items 和不可见内容标记，调用方必须完成遍历才能确认不存在；已知 item 的
Status 按字段 ID 读取。add Issue 与 set Status 是两次独立 mutation，均禁止 SDK 自动重试。
HTTP 200 中的 GraphQL errors/partial data 是失败，异常仅显示分类与 endpoint，不打印 token。
F8 提供接口；持久的响应丢失核对和 Flow 自动同步由 F9 接入。

依据：[Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)、
[组织 Projects 权限](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)、
[字段与 item schema](https://docs.github.com/en/graphql/reference/projects)。
真实组织 Project 尚未配置，真实 App 只读 smoke 未验证；自动化覆盖受控响应和真实 githubkit transport。
