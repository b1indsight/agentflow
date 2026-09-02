# M1 GitHub App 与临时能力计划

> 状态：待审批 · 计划日期：2026-09-02 · M1 Feature 2/3
>
> 前置：Feature 1 固定 CodeAgent 对本地 VCS、commit 与 push 的 ownership。本 feature 校准 GitHub App
> 并提供真实 Issue、远端 ref、短时 push capability 和 PR primitives；完整 Issue→PR 编排属于 Feature 3。

## 1. 背景与目标

当前 `GitHubApp` 写出了 githubkit 的常见调用形态，但认证方法、返回类型、权限和错误行为都没有用真实
App 校准。`JobCoordinator` 仍使用占位 Issue 标题、空正文和硬编码 `main`，`installation_token()` 也没有
实现仓库/权限收窄。现有代码不能仅通过填入私钥就视为可用。

本 feature 建立 control plane 可依赖的 M1 GitHub 能力：

1. 按 Webhook 保存的 installation id 创建已校准的 githubkit client；
2. 读取真实 Issue 标题/正文和仓库默认分支；
3. 为单个目标仓库生成短 TTL、最小权限的 push capability；
4. 查询远端 branch/ref 和按 head 查找已有 PR；
5. 创建 PR、评论、设置状态 label、请求显式配置的 reviewers；
6. 把认证、权限、not found、冲突、rate limit 和暂时故障映射为稳定的 adapter 错误；
7. 证明 secret 不进入数据库、日志、持久 job payload 或通用模型表示。

完成后，control 能针对一个 disposable GitHub 仓库独立完成 M1 所需的读写 smoke，但尚不派发 Codex
INITIAL task，也不宣称 Issue→PR 闭环完成。

## 2. 范围与非目标

### 2.1 本次包含

- 对锁定 githubkit 版本的 App/installation 认证、REST 与必要查询接口做实测校准；
- `IssueSnapshot`、`RepositorySnapshot`、`RemoteRef`、`PullRequestSnapshot` 等最小类型化结果；
- repository-scoped installation token/capability，权限只覆盖 M1 push 所需内容；
- get issue、get repository/default branch、get ref、find PR by head、open PR；
- issue/PR comment、labels、reviewers 的 M1 写操作；
- 独立 `reviewers` 配置，不复用允许触发 flow 的 `allowlist`；
- adapter contract 测试、错误分类测试和一次 disposable repo 的真实 GitHub App smoke；
- GitHub App 权限、Webhook 订阅、secret 生命周期和 smoke 操作文档。

### 2.2 本次不包含

- CodeAgent prompt、workspace、commit/push 命令或 Codex sandbox 实现；
- WebSocket assignment 下发 secret；该生命周期在 Feature 3 接线；
- review thread list/resolve 的真实校准、GraphQL 分页或 M2 triage/revision；
- Checks/commit status、Project v2、merge、close issue、archive 或 reconcile；
- 通用 GitHub SDK 封装、完整 API surface 或跨 forge 支持；
- 对所有 GitHub rate-limit/abuse 场景自动重试；
- 自动创建 GitHub App、修改组织权限或安装到未授权仓库。

## 3. Port 与类型化能力

### 3.1 M1 读取模型

`GitHubClient` 的 M1 方法返回稳定的内部模型，而不是把 githubkit schema 泄漏给 coordinator：

- Issue snapshot：number、title、body、state；
- Repository snapshot：`owner/name`、default branch；
- Remote ref：完整 branch 名和 commit SHA；
- PR snapshot：number、state、head branch/SHA、base branch；
- Push capability：secret value、仓库范围、权限和 expiry，只在 secret-aware 边界使用。

内部模型保留 M1 真正需要的字段。未来 review/CI/reconcile 扩展新模型，不把未使用的 GitHub payload
提前复制进 core。

### 3.2 M1 操作

最小 port surface：

1. `get_issue(installation, repo, number)`；
2. `get_repository(installation, repo)`；
3. `create_push_capability(installation, repo)`；
4. `get_ref(installation, repo, branch)`；
5. `find_pull_request_by_head(installation, repo, branch)`；
6. `open_pull_request(...)`；
7. `comment(...)`、`set_labels(...)`、`request_reviewers(...)`。

方法名可按项目风格调整。关键约束是 core 只依赖类型化 port，githubkit 的认证与返回结构全部留在
adapter 内；`open_pull_request` 不实现自动 merge。

## 4. 认证、权限与 secret 生命周期

### 4.1 认证链

serve 持有 App id 和私钥，通过 App JWT 换 installation token。所有 API 调用都使用 flow 上保存的
installation id，不接受 worker 自报 installation。私钥永远不离开 control plane。

启动时只验证配置形状和私钥可读；真实安装/权限在首次 smoke 或请求时校准。错误不得静默降级到个人
token。

### 4.2 Push capability

push capability 必须：

- 只授权当前 `owner/repo`；
- 请求 Contents read/write，不申请 Issues/PR 写权限给 CodeAgent；
- 使用 installation token 的实际 expiry，不自行宣称更长 TTL；
- 不写入 Store、job payload、result inbox、worker journal、workspace、Codex prompt 或日志；
- 只在 Feature 3 的 assignment 临近发送时创建，失败/retry 时重新签发；
- 用于 HTTPS Git credential helper，不能把 App private key 交给 worker。

能执行 push 的 CodeAgent 原则上可能读取该 capability；安全边界依赖 repo scope、短 TTL、受限网络、
branch protection 和 allowlist，而不是不可实现的“对执行进程绝对隐藏”。

### 4.3 权限与配置

实施时逐 endpoint 对照 GitHub 官方文档确认最小权限。预期 App 权限：

- Metadata: read；
- Contents: read/write；
- Issues: read/write；
- Pull requests: read/write。

`FLOWTOOL_ALLOWLIST` 只决定谁能触发。新增独立 reviewers 配置，空列表表示不请求 reviewer；不得把所有
可触发用户自动变成 reviewer。状态 label 名称也使用显式配置或稳定默认值。

## 5. 错误与幂等语义

adapter 把外部错误分为：

- authentication / permission：配置或安装错误，fail closed，不盲目重试；
- not found：repo、issue、ref 不存在，交给 coordinator 形成明确失败；
- conflict / validation：例如 branch 不存在、PR 参数非法；
- rate limited / transient：保留 retry-after/是否可重试信息；
- unexpected response：包含 endpoint 和 request id，不包含 secret/body 中的敏感数据。

本 feature 提供按 head branch 查找 PR 的能力，但“先查后开并在崩溃重放时复用”的编排放在 Feature 3。
labels 和 reviewers 使用 GitHub 自身幂等接口；评论本 feature 不承诺 exactly-once。

## 6. 模块与文件布局

- `app/ports.py`
  - 以类型化 M1 方法扩展 `GitHubClient`，保留 M2 thread 方法但不在本 feature 校准。
- `app/models.py` 或新的轻量 GitHub domain 模块
  - 放置不含 SDK 类型的 snapshot/error/capability 模型；secret 模型禁止默认 repr/序列化。
- `app/github_client.py`
  - 校准 githubkit client、installation token、REST 请求和响应转换；
  - 集中 owner/repo 解析、request id、错误映射和日志脱敏。
- `app/config.py` / `env.example`
  - reviewers、状态 label 和 App 配置验证。
- `tests/`
  - fake port、adapter transport/response、配置和脱敏测试。
- `docs/architecture/github-app.md`
  - 更新已验证 endpoint、权限矩阵、smoke 前置与剩余 M2+ 缺口。

若 githubkit 不提供稳定的 repository-scoped token 参数，adapter 可对该单一 endpoint 使用 GitHub 官方
REST 请求；不得为了形式统一牺牲权限收窄或返回校验。

## 7. 实施顺序

1. **Port 与错误契约，测试先行**
   - 用 fake client 固定 snapshot、capability 和错误分类；
   - 更新 coordinator tests 的 fake 实现，但不接真实 INITIAL 流程。
2. **认证和读取能力，测试先行**
   - 校准 installation client、Issue、repository/default branch 和 ref；
   - 覆盖空 body、默认分支非 `main`、缺失 repo/issue 和权限错误。
3. **Scoped capability，测试先行**
   - 固定 repo/permission/expiry 请求与 secret-aware 类型；
   - 证明 repr、异常、日志和普通序列化不会暴露 token。
4. **PR primitives，测试先行**
   - find-by-head、open PR、comment、labels、reviewers；
   - 覆盖 head/base、已有 PR、空 reviewers 和 API validation error。
5. **配置、真实 smoke 与文档**
   - 在用户授权的 disposable repo 运行读写 smoke；
   - 记录 App 权限、installation/repo、endpoint 结果和清理方式，不记录 token；
   - 同步 GitHub App 文档和计划实施结果。

## 8. 测试与验收

自动化测试：

- githubkit 返回对象被转换为内部 snapshot，不向 core 泄漏 SDK 类型；
- repo 默认分支不是 `main` 时保持真实值；
- capability 请求明确限制单 repo 与 Contents 权限，并保留真实 expiry；
- token 不出现在 repr、日志、异常、Store/job/result/journal fixture；
- find PR by head 能区分无结果、单一结果和歧义/异常响应；
- trigger allowlist 与 reviewers 配置完全独立；
- auth、permission、not found、validation、rate limit、transient 映射稳定；
- 现有 fake GitHub coordinator/router tests 继续通过。

真实 GitHub App smoke 在 disposable repo 验证：

1. 用 installation id 读取真实 Issue 与非硬编码默认分支；
2. 生成只针对目标 repo 的短时 capability，并验证声明的权限/expiry；
3. 查询一个测试 branch 的远端 SHA；
4. 创建一张测试 PR，读取其 head/base，再执行 label/reviewer/comment 中已配置的操作；
5. 人工关闭/清理测试 PR 和 branch；自动化不删除用户数据；
6. 日志和测试产物搜索不到 token 或 private key 内容。

常规验证命令：

```bash
uv run pytest
uv run ruff check app tests
uv run mypy app tests
uv lock --check
uv build --no-sources
git diff --check
```

## 9. 风险与回退

- **githubkit API 漂移**：锁文件版本是事实源；以 adapter tests 和真实 smoke 校准，不根据旧示例猜方法名。
- **权限过宽**：token 创建必须显式 repo/permission；无法收窄时本 feature 不通过验收，不回退到 PAT。
- **secret 泄漏**：secret-aware 类型、集中脱敏和负向搜索测试三层防护；泄漏视为阻断问题。
- **外部副作用重放**：Feature 2 只提供 primitives；Feature 3 必须用 remote ref/head PR 查询收口幂等。
- **真实 smoke 污染仓库**：只使用用户指定 disposable repo 和唯一前缀；删除/关闭仍由用户确认或人工完成。
- **rate limit/retry 过度设计**：保留结构化 retry 信息，通用 backoff/reconcile 延后到 M4。

若真实 App 证明候选权限或 githubkit endpoint 不成立，在同一计划 PR 更新实际权限/API 决策后再继续；
不得以“测试用了 fake”替代外部校准。
