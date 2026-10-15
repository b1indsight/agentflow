# 单账号 Cloudflare Access 接入 — Feature record

**Stage:** Implemented locally（自动化验证通过，真实 Access 部署待验收）
**PR:** 待创建实现 PR；真实部署验收仍未完成。

## 1. Initial plan

### Summary and motivation

用户要求增加权限和账号安全校验，并确定首版只允许自己登录。当前对话 API 使用
`FLOWTOOL_CONTROL_API_TOKEN` 共享 Bearer Token，没有个人登录身份。拟采用 Cloudflare Access
完成 GitHub 登录和 MFA，应用校验 Access JWT 并限定单个 owner 邮箱。

基线为已合并架构文档更新的 main。现有实现见
[API](../../app/controler/api.py)、[配置](../../app/controler/config.py)和
[部署入口](../deployment/README.md#3-配置-cloudflare-入口)。

### Goals, scope, and success criteria

- 只允许 owner 登录并读取用户 API；不增加注册、密码、多用户、RBAC 或账号数据库。
- Access 只启用 GitHub 登录，Allow 策略限定 owner 的确切邮箱，并要求独立 MFA。
- 后端独立验证 JWT 和 owner 身份；其他有效 Access 用户、机器身份及伪造 header 均不能访问。
- 移除用户 API 的共享 Bearer Token 入口，不保留其作为绕过账号校验的备用路径。
- GitHub Webhook、Worker、metrics 和探针继续使用各自边界，不统一套上用户登录。
- 不新增控制台 UI；不修改现有 GitHub 事件操作者授权规则，本计划仅覆盖用户 HTTP 入口。

### Proposed behavior

部署时配置 Access team issuer、应用 audience 和唯一 owner 邮箱。缺失配置时用户 API 关闭，
不降级放行。浏览器先完成 GitHub 登录和 MFA；应用从 `Cf-Access-Jwt-Assertion` 读取 JWT，
校验成功且属于 owner 才允许继续查询。

- 缺失、伪造、过期或签发方/audience 错误的 token：401。
- 有效 token 的邮箱不匹配，或属于 Service Token 机器身份：403。
- 必要配置缺失或公钥无法可靠取得：503，不执行对话读取。
- 仅提供旧 Bearer Token、邮箱 header 或任意 Cookie 不构成认证。
- 拒绝响应也设置 `Cache-Control: no-store`；日志不记录 JWT、Cookie、私钥或完整身份内容。

### Technical design and affected modules

- `app/controler/config.py`：增加 issuer、audience、owner email 配置和成组配置校验，
  删除 `control_api_token`。邮箱是部署配置，不把猜测的邮箱或 GitHub 用户名写成默认值。
- 控制端认证模块：固定 RS256；校验签名、issuer、audience、有效期及必要身份字段。
  只从配置的 HTTPS team issuer 获取 JWKS，不使用 token 中的任意 URL；处理公钥缓存、
  轮换、有界超时和未知 kid，防止每个恶意请求都触发无界公钥请求。
- `app/controler/api.py`：用户 API 在业务查询前通过统一认证依赖；Webhook HMAC、
  Worker token、metrics token、探针保持独立。FastAPI 自动生成的文档入口仍不公开。
- `pyproject.toml` / `uv.lock`：实现时核对已安装 JWT 库，显式声明实际使用的库和加密能力，
  不手写签名算法或依赖未声明的传递依赖。
- 更新现有对话 API 测试、配置测试和部署示例；不修改会话协议和数据库 schema。
- 部署文档新增独立管理域名，只转发明确允许的用户 API 路径；webhook 域名继续仅公开
  `/webhook`，其余路径返回 404；Worker/metrics/探针不经过管理公网域名。
- 源站只在受控网络中可达，防止绕过 Access。应用 JWT 验证为额外保护，不替代网络限制。

### Alternatives and rationale

- 继续共享 Bearer Token：缺少个人登录和 MFA，不能满足本次账号校验目标。
- 自建 GitHub OAuth、session 和 MFA：引入更多凭据与会话维护，本阶段不采用。
- 只在 Cloudflare 配白名单：部署简单，但应用无法独立拒绝错误 audience 或其他账号的
  token；采用边缘策略和应用 JWT/owner 校验两层限制。
- 首版不绑定 GitHub repo 权限；唯一 owner 可访问此部署下所有被追踪 Flow。

### Costs, risks, and mitigations

- 登录依赖 Cloudflare Access、GitHub 与已部署域名；本地无 Access 时用户 API 默认不可用。
- MFA 和指定 GitHub 登录方式由 Access 策略执行；JWT 校验本身不证明这些策略已正确配置，
  必须在真实部署验收中检查。邮箱不等于 GitHub username，须确认 GitHub 登录实际提供的邮箱。
- 公钥轮换和短暂网络故障会影响认证可用性；使用有界缓存和超时，无法校验时拒绝访问。
- 旧对话 API 客户端需改为登录后访问，本项目尚未部署，不提供旧 token 兼容层。
- 本次不改变 GitHub webhook 事件授权，Access 不会为事件中的 GitHub actor 自动授予权限。

### Implementation order

1. 确认 owner 邮箱及 Access team/application 配置；批准本计划。
2. 实现配置和 JWT/owner 验证，替换对话 API 认证并删除旧入口。
3. 更新契约测试、部署说明、Docker/K8s 配置引用及过时 token 文档。
4. 执行自动化验证，再在授权的部署环境验证 GitHub 登录、MFA 和未授权账号拒绝。

### Validation and ablation strategy

- **Tests:** 通过 HTTP 入口验证 owner 可访问；其他邮箱、机器身份、伪造邮箱 header、
  旧 Bearer Token、过期/尚未生效/签名错误/错误 issuer/audience token 均拒绝，且不调用业务读取。
  覆盖 JWKS 轮换、未知 kid、取钥失败与并发请求边界，以及缺失/非法配置。
- **Regression:** 对话分页、断连取消、Webhook 签名和 Worker/metrics 认证不受影响；
  执行相关 pytest、Ruff、Mypy 和配置模板检查。
- **Ablation:** 用隔离的临时测试分别省略 owner 匹配和 audience 校验，确认其他账号及其他
  应用 token 会突破对应负向用例。比较结果记录在实施更新中，临时变体不保留在产品代码里。
- **Timing:** 实施后、代码审查前执行。真实 GitHub 登录/MFA 与源站不可绕过需部署验收，
  不把本地签名测试报告为公网验收成功。

### Open questions and deferred work

- **待确认配置：** owner 在 GitHub 登录时提供给 Access 的邮箱、Access team domain、
  application audience、管理域名。均不属于密码或密钥；不推测用户邮箱。
- **待批准：** 本计划及移除旧共享 Bearer Token 路径。
- **后置：** 多用户、仓库级授权、控制台 UI、写操作及其 CSRF 防护、自动化客户端身份。
  首版用户 API 不接受 Service Token，也不新增机器登录入口。
- 本计划不授权修改 Cloudflare 账号配置或创建 GitHub OAuth App；实际部署改动另行确认。

## 2. PR change timeline

### [001] — 2026-10-15 — 单 owner HTTP 认证与部署边界落地

- **实际变更：** 新增 `app/controler/access.py`，对话 API 使用统一 owner 认证依赖；
  Runtime 管理验签客户端并在关闭时释放。删除旧 `control_api_token` 字段、Bearer 路径和
  Worker 中相应旧引用，显式声明既有 PyJWT crypto 依赖；数据库和 Worker 协议未改变。
- **设计决定：** issuer 限定 HTTPS Cloudflare team domain；三项配置必须成组，默认关闭
  用户 API。只接受一个 assertion header，拒绝机器身份；邮箱比较忽略大小写。
  JWKS 响应限制 64 KiB/16 个公钥，仅允许至少 2048 位的 RSA 公钥；验签固定 RS256。
  公钥缓存一小时、刷新全程五秒超时，并串行化、每分钟最多一次刷新。
- **权衡：** 未知 kid 的轮换在冷却期间最多等待一分钟；公钥缓存过期且取钥失败时关闭访问，
  不降级使用过期缓存。仍有效的已知缓存不因其他 kid 的失败请求而被删除。
  owner 邮箱、team、audience 和管理域名继续留作部署配置，不因缺少真实值阻塞本地实现。
- **部署与文档：** 更新会话 API/组件职责、env 示例、K8s env 说明和 Changelog，新增
  [Access 操作说明](../deployment/access.md)。Docker/K8s 模板提供默认注释的精确管理路由，
  保留 webhook-only 默认值，不公开整个 origin，不自动修改 Cloudflare/GitHub 账号配置。
- **验证：** 新认证测试先运行，因认证模块尚不存在而失败；实施后覆盖 owner 成功、
  其他邮箱/机器身份、签名/issuer/audience/时效错误、伪造 header/Cookie、旧 Bearer、重复
  header、并发取钥、轮换、未知 kid 冷却、过期缓存/取钥故障和畸形/超大 JWKS。
  原会话分页和 Worker 忙碌的四阶段端到端测试切换为真实本地 RSA 签名后通过。
  全量回归发现手工构造 Runtime 的关闭测试缺少新增客户端，已补齐装配并验证关闭调用。
- **完整检查：**
  `uv run --locked --no-sync --env-file .cache/2026-10-13/persistence-test.env pytest -q -p no:cacheprovider`
  → 768 passed，唯一 warning 为既有 Starlette TestClient/httpx 弃用提示，无跳过；
  `uv run --locked --no-sync ruff check app tests`、
  `uv run --locked --no-sync mypy app tests`、`uv lock --check`、`uv build --no-sources`
  和 `git diff --check` 均通过。
- **消融结果：** 临时实验通过相同 FastAPI HTTP 入口发送 owner、其他用户、其他应用三类
  本地签名请求；基线分别返回 200/403/401，拒绝场景业务读取次数为零。
  仅去掉 owner 比较，其他用户变为 200 且读取一次；仅关闭 audience 校验，其他应用变为
  200 且读取一次。两项机制均保留；实验只在内存替换函数，未修改产品源码，临时脚本已删除。
  本线程保留 `.amp/in/artifacts/access-ablation.json` 和 `access-pytest.txt`，不提交仓库。
- **模板检查：** 解析 Docker/K8s Tunnel YAML，验证默认仅 Webhook，启用注释路由后仅匹配
  正整数 Flow 的 conversation 路径；Worker、metrics、探针、docs 等不匹配。15 个本地
  文档链接目标存在。当前机器无 cloudflared/kubectl，未运行 Tunnel 原生规则校验或集群安装。
- **未完成部署验收：** 未配置用户邮箱、Access team/audience、OAuth App 或真实域名；
  未运行真实 GitHub 登录/MFA、Cloudflare 策略拒绝其他账号和源站网络不可绕过的验收。
  当前只有本地实现，无 commit、push、PR 或上线；这些状态不由自动化通过推断。

## 3. References

- [GitHub 登录集成](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/github/)
- [Access 策略](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)
- [独立 MFA](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/independent-mfa/)
- [JWT 校验](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
