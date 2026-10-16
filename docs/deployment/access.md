# 单账号登录：Cloudflare Access

用户 HTTP API 使用 Cloudflare Access 的签名身份凭证，并且只允许部署配置中的一个 owner
邮箱。当前只有只读[会话查询 API](../architecture/conversation-api.md)，不包含 Web 控制台。
后端认证已提供；真实域名、GitHub 登录和 MFA 必须在部署环境完成下面的配置和验收。

## 1. 创建登录与访问策略

在你的 GitHub 账号创建独立 OAuth App，按
[Cloudflare 的 GitHub 集成说明](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/github/)
设置 team domain 和 callback。Client Secret 直接填入 Cloudflare 的身份提供方设置，
备份留在密码管理器；不放进 AgentFlow、Worker 或 Git。它与执行仓库操作的 GitHub App 分开。

在 Access 创建 Self-hosted application，保护整个管理域名（例如 `admin.example.com`）：

- 登录方式只启用 GitHub。
- Allow 策略只 Include 你的确切邮箱，使用 GitHub 登录实际返回的邮箱。
- 要求 [Independent MFA](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/independent-mfa/)，
  可使用安全密钥、Touch ID 等 WebAuthn 设备或 TOTP；准备备用验证方式以防锁号。
- 不给管理域名添加 Bypass、Everyone、Service Auth 或其他扩大访问范围的 Allow 策略。
- 设置合适的会话时长；紧急收回访问时在 Access 撤销会话，并关闭管理路由或用户 API。

[Access 策略](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)的登录方式和 MFA
由边缘执行，后端验签不能证明这些策略已经配好。上线前必须实际核对，不能只配置邮箱便宣告安全验收。

## 2. Control 配置

只在 serve 的私有 env 文件设置三项，替换示例值：

```dotenv
FLOWTOOL_ACCESS_ISSUER=https://YOUR-TEAM.cloudflareaccess.com
FLOWTOOL_ACCESS_AUDIENCE=YOUR_APPLICATION_AUD_TAG
FLOWTOOL_ACCESS_OWNER_EMAIL=you@example.com
```

issuer 必须是 HTTPS team domain，不带端口、路径或末尾斜杠；audience 使用该 Access 应用的
AUD tag。这三项不是密钥，但要保护其修改权限。原生 env 文件权限设为 600；Docker 放部署
目录的 serve.env；Kubernetes 放 serve 专用 env Secret。均不提供给 Worker。

三项均为空时用户 API 返回 503；只配置部分或配置格式错误时 serve 启动失败。
旧 `FLOWTOOL_CONTROL_API_TOKEN` 已删除，旧 Bearer Token 不再授权用户 API。
Control 不需要 Cloudflare 管理 API Token、OAuth Secret、MFA 密钥或 JWT 签名私钥。

## 3. 管理域名和源站边界

先完成 Access 策略，再创建管理域名的 Tunnel DNS 路由，并在 catch-all 前添加精确路由。
原生部署示例（用实际域名替换）：

```yaml
ingress:
  - hostname: hooks.example.com
    path: ^/webhook$
    service: http://127.0.0.1:8000
  - hostname: admin.example.com
    path: ^/api/flows/[1-9][0-9]*/conversation$
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Docker 和 Kubernetes 的 Cloudflare 模板提供了默认注释掉的管理路由，按实际 origin 启用。
源站只监听 loopback 或受控内部网络；不额外向公网开放 serve 端口。不要公开整个应用，
`/docs`、`/openapi.json`、Worker、metrics 和探针均不通过管理域名暴露。
Webhook 使用另一个域名，不套用户登录，仍必须通过 GitHub HMAC；Worker 使用内部 WSS/token。
隧道凭据仅挂给 cloudflared，浏览器登录 Cookie 不存入 AgentFlow 数据库。

重载 Tunnel 前，使用现有 cloudflared 的 ingress validate/rule 命令检查两类域名和 404 兜底。
重启 serve 以加载认证配置；Docker/K8s 使用同版本镜像与已有受控更新流程。

## 4. 后端验证与故障行为

浏览器访问管理域名，Access 完成 GitHub 登录与 MFA，并向源站发送
`Cf-Access-Jwt-Assertion`。Control 只读取这一 header，拒绝重复 header；Cookie、邮箱 header
和共享 Bearer Token 不直接构成后端认证。

Control 固定使用 RS256，验证签名、issuer、audience、exp、iat 和可选 nbf，要求应用 token、
非空用户 sub 和 owner 邮箱（忽略邮箱大小写）。拒绝机器 Service Token。
公钥只从配置 team 的 `/cdn-cgi/access/certs` 获取，不跟随重定向或 token 自带的 URL。
公钥缓存一小时，刷新请求全程限时五秒，响应最多 64 KiB、16 个 RSA 公钥，密钥至少 2048 位。
刷新串行化，每分钟最多一次，包括失败和未知 kid；轮换发生在冷却期间时，新 kid 最多需等
一分钟再重试。刷新失败不清除仍有效的已知公钥；缓存过期后绝不使用旧公钥降级放行。
缓存期限内，已缓存公钥的撤回可能要等到刷新才能在源站体现，因此必须保持 Access 和网络边界。

后端响应：无效身份凭证 401，其他邮箱/机器身份 403，关闭用户 API 或取钥不可用 503。
所有用户 API 响应均为 `Cache-Control: no-store`。不记录 JWT、Cookie 或完整身份内容。
参见 [Cloudflare 官方验签说明](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)。

## 5. 部署验收

- 浏览器未登录时进入 Access 登录；你完成 GitHub 登录与 MFA 后可查询已存在 Flow。
- 用其他 GitHub 账号登录被 Access 拒绝；不只是依赖应用返回空数据。
- Access 配置确实只允许 GitHub、指定邮箱并要求 MFA；不用本地签名测试替代此项。
- 公网 `/docs`、`/openapi.json`、`/metrics`、探针和 Worker 路径返回 404。
- 未认证直连源站的 API 被拒绝，公网无法绕过 Tunnel 直接访问 serve。
- 原 webhook 域名签名错误返回 401、合法 delivery 返回 202，Worker 内部连接正常。

当前没有用户真实域名或 Access 配置的验收记录；部署参数和上述结果须由实际环境补齐。
