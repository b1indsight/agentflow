# GitHub App 接入

> 状态：已对齐 githubkit 0.16 并通过隔离契约测试；尚未在真实 App 上执行读写 smoke。

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

订阅事件候选：Issues、Issue comment、Pull request、Pull request review、Pull request review comment。
GitHub 会根据已选权限决定哪些事件可订阅；正式接入时应逐事件核对
[Webhook events and payloads](https://docs.github.com/en/webhooks/webhook-events-and-payloads)。

安装 App 时只授权目标仓库。记录 App ID、私钥路径、Webhook secret；installation ID 从 Webhook
payload 获取，不需要静态写入 `.env`。

## A.3 已实现能力与缺口

`GitHubApp` 启动时验证正 App ID 和可读非空私钥，按 Webhook 保存的 `installation_id` 构造 githubkit
0.16 client。core 只接收类型化的 Issue、repository、remote ref、PR snapshot 和 secret-aware push
capability，不接触 githubkit schema。

已由自动化覆盖：

- Issue、repository/default branch、ref、按 head 查找 PR、创建 PR、评论、labels 与 reviewers 的方法；
- 通过公开 installation access-token endpoint 显式传入单仓库和 `contents: write`，并校验返回 scope；
- authentication、permission、not found、conflict、validation、rate limit、transient 与异常响应分类；
- capability 的 token 在 repr 和普通 JSON 序列化中脱敏。

尚未验证或实现：

- disposable repository 上的真实 App 读写 smoke；
- 用 credential helper 注入 token、限制网络到 GitHub、使用后撤销/清理且不写入工作区；
- review thread ID 映射、Project v2 与 reconcile 查询。

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
