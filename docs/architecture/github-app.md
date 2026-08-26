# GitHub App 接入

GitHub App 与 PAT 的本质区别：它是一个**有独立身份的机器人**，认证不是一把固定 token，
而是"**私钥签发临时 token**"的两段式。工具的评论以 `flowtool[bot]` 名义发出，和你自己的 review 一眼分得清。

## A.1 三种 token

```
私钥(.pem) + App ID
   │  用私钥签 RS256 JWT（最长 10 分钟）
   ▼
App JWT ──POST /app/installations/{installation_id}/access_tokens──► Installation Token（1 小时，限定权限+repo）
                                                                          │
                                                          以 flowtool[bot] 身份调 API + git push
```

- **App JWT**：证明"我是这个 App"，只用来换下面的 token（管不了具体 repo）。
- **Installation Token**：真正干活的 token，1 小时过期、仅在你授权的 repo + 勾选权限内有效；工具缓存、快过期再换。
- **User token**（OAuth 代表某用户）——**本工具用不到**。

## A.2 一次性设置（网页操作一次）

1. **注册 App**：`github.com/settings/apps → New GitHub App`（个人账号即可）。填：
   - **Webhook URL**（事件推送地址）+ **Webhook secret**（自定，用于验签）
   - **Permissions**（细粒度，只给需要的）：
     - Contents: **Read & write**（push 分支 / 提交）
     - Issues: **Read & write**（评论、打标签、关闭）
     - Pull requests: **Read & write**（建 PR、评论、请求 reviewer）
     - Metadata: **Read**（强制）
     - Checks / Commit statuses: **Read**（想读 CI 状态才要）
   - **Subscribe to events**：Issues、Issue comment、Pull request、Pull request review、Pull request review comment
2. **生成并下载私钥**（`.pem`）；记下 **App ID**。
3. **安装到目标 repo**：App 页 → Install → 勾选 repo，得到 **installation id**（webhook payload 里也带 `installation.id`）。

产出四样：`App ID` · `私钥.pem` · `Webhook secret` · `installation id`。

## A.3 运行时流程

1. Webhook 进来 → 用 **Webhook secret** 验 `X-Hub-Signature-256`（HMAC-SHA256 原始 body）→ 读 `installation.id`。
2. 用 **App ID + 私钥** 签 JWT → 换 **installation token**（缓存 ~55 分钟）。
3. 用该 token 调 REST/GraphQL；**git push 也用它**：
   `https://x-access-token:<token>@github.com/<owner>/<repo>.git`

`githubkit` 内置 App 认证（给 `app_id / private_key / installation_id`，自动签 JWT、换 token、到期续），
并自带 webhook 验签与 typed 事件模型——不用手搓。

## A.4 本地开发的 webhook

本机无公网，用 **smee.io**：建 channel 得到公网 URL，填进 App 的 Webhook URL，
本地跑 `smee -u <channel> -t http://localhost:8000/webhook` 转发到 daemon。生产再换真实公网地址。

## A.5 密钥管理

`私钥.pem` / `Webhook secret` 走环境变量或本地 secret 文件，**不入库、不进 git**（`.gitignore` + `env.example` 占位）。
