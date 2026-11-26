# GitHub App 接入：谁收事件，谁能改仓库

给 Issue 加上 `flow` 标签后，GitHub 把一条事件投递给 control。接下来有三个独立判断：请求是否来自持有 Webhook secret 的发送方，加标签的人能否启动 Flow，以及这个 App 的安装实例（installation）能否访问目标仓库。分别做这三个判断，才能让 worker 在指定仓库交付代码，同时把创建 PR、回复评审和请求复审留给 control。[事件入口](../../app/controler/api.py) · [触发规则](../../app/controler/events/router.py) · [任务凭据签发](../../app/controler/workflow/coordinator.py)

## A.1 认证链

Control 使用 `AGENTFLOW_GITHUB_APP_ID` 和 `AGENTFLOW_GITHUB_PRIVATE_KEY_PATH` 初始化 [GitHubApp](../../app/github/apps.py)，要求 ID 为正数、私钥可读且非空。SDK 用私钥签短期 [App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)。Control 以 App 身份读取自身的 slug，得到 `slug[bot]`，并可换取[安装令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)。安装令牌通常一小时过期，权限不能超过该 installation 已获授权的仓库与操作。

对于标签触发的 Flow，[路由器](../../app/controler/events/router.py)从事件取 `installation.id`，和仓库、Issue 一起交给 control。此后 GitHub 请求沿 `GitHubApp → Installation → Repository → Issue / PullRequest` 找到操作对象。绑定资源时还没联网；持有一个 PR 编号也不等于知道它是否仍开放。[PR 资源](../../app/github/pull_requests.py)将已读取的事实与未读取的字段区分开；交付前仍需复读 PR 和分支。

## A.2 注册与最小权限

将 App 安装到需要处理的仓库，配置公开 HTTPS 地址 `/webhook`、JSON payload 和与 `AGENTFLOW_GITHUB_WEBHOOK_SECRET` 相同的 secret。事件订阅选择 **Issues、Issue comment、Pull request、Pull request review、Pull request review comment**。如果以专门的仓库分发 skills，再授权该仓库并订阅 **Push**：配置的默认分支更新会通知执行 worker 刷新，已有 Flow 保留原快照。[部署配置](../deployment/README.md) · [消费者](../../app/controler/events/webhooks.py) · [GitHub 事件目录](https://docs.github.com/en/webhooks/webhook-events-and-payloads)

| 授权范围 | 谁需要，用来做什么 |
|---|---|
| Repository Metadata: read；Contents: read/write | 识别仓库、读分支与提交、通过 HTTP Git 推送。下发给 worker 的权限另行收窄。 |
| Issues: read/write | Control 读取 Issue、管理标签、回写讨论。 |
| Pull requests: read/write | Control 创建或复用 PR，读取评审，回复或解决线程，请求 reviewer。 |
| Checks: read；Commit statuses: read；Actions: read | 控制台读取检查与提交状态；Actions 权限用于按 check suite 补全工作流名称。名称查询失败仍显示已读到的检查，附缺失原因。 |
| Organization Projects: read/write（可选） | 将 Issue 卡片的 Status 同步到已配置的组织看板。 |

权限增加需要安装方批准，服务不会替用户扩权。参看 [GitHub 权限说明](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)、[控制台检查读取](../../app/github/console.py)。`AGENTFLOW_ALLOWLIST` 决定谁能通过标签启动任务，也用于识别可直接进入修订的 Request Changes；`AGENTFLOW_REVIEWERS` 决定交付后向谁请求评审，留空就不请求。这两份名单都不改变 App 权限。[服务配置](../../app/controler/config.py) · [评审循环](review-loop.md)

## A.3 仓库标签：建好定义和触发任务是两件事

`AGENTFLOW_TRIGGER_LABEL`、`AGENTFLOW_IN_REVIEW_LABEL`、`AGENTFLOW_DONE_LABEL` 默认分别是 `flow`、`flow:in-review`、`flow:done`；三个名字不能为空，也不能忽略大小写后重名。[后台扫描器](../../app/controler/reconciliation/labels.py)约每 300 秒枚举有效安装及非归档仓库，完整读取标签后补缺，不改已有颜色、描述，也不代人给 Issue 加标签。确认齐全后每 24 小时复查，重启后重新确认。限流会延后扫描；`/readyz` 正常不保证标签已建好。[标签核对测试](../../tests/test_github_labels.py)

真正启动任务的是白名单用户的 `labeled` 事件；移除触发标签会取消该 Issue 上活动的 Flow。系统回写评审、完成状态时只增删对应标签，保留用户的其他标签。[事件路由](../../app/controler/events/router.py) · [标签操作](../../app/github/issues.py)

## A.4 Webhook 校验

[HTTP 入口](../../app/controler/api.py)对**原始字节**计算 HMAC-SHA256，用 `compare_digest` 比较 `X-Hub-Signature-256`。先限体积、再验签，最后检查 delivery ID、事件名和 JSON 对象。超长返回 413，签名错返回 401，非法格式返回 400。相同投递可去重，同一 ID 的内容冲突返回 409，收件数据库不可用返回 503。[GitHub 验签规范](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

**202 只确认收件。** [收件箱](../../app/persistence/webhooks.py)先持久化投递，[消费者](../../app/controler/events/webhooks.py)再领取，在同一数据库事务里路由事件、记录控制台观察、完成收件记录；事务提交后才唤醒 worker。中途崩溃可重新领取，旧领取者不能确认新的领取。即使进程收到的是合法签名，bot 自己发表的评论也不会再启动一次评审。[收件箱回归测试](../../tests/test_webhook_inbox.py) · [查看与重试投递](../operations/webhook-inbox.md)

例如，给 `owner/repo#7` 加标签后，若 GitHub 再次投递同一事件，可以沿两个调用点判断该由谁重试：

1. [HTTP 入口](../../app/controler/api.py#L244-L253)经 [ServerRuntime.receive_webhook](../../app/controler/runtime.py#L300-L302) 调用 [WebhookStore.receive](../../app/persistence/webhooks.py#L38-L61)，传的是 **delivery ID**、事件类型、解析后的对象和**原始字节**的 SHA-256。正常返回没有 Flow ID：新收件已提交，或同一投递早已收下。重复请求不会重置处理进度。
2. [WebhookConsumer.sweep](../../app/controler/events/webhooks.py#L53-L109) 调用 [WebhookStore.claim](../../app/persistence/webhooks.py#L63-L95)，拿到带领取凭据的 `Webhook`；`None` 表示此刻没有可领的投递。消费者在业务事务中用 `lock(item)` 核验领取权，再调用 `EventRouter.dispatch`；`complete(item)` 成功才能连同业务修改提交。处理失败由消费者调用 [WebhookStore.fail](../../app/persistence/webhooks.py#L97-L107) 安排退避或标记 `blocked`。已提交后即使唤醒失败，也由周期扫描接手，无须重新加标签。

## A.5 密钥管理

App 私钥和验签 secret 留在 control。临近下发编码任务时，control 单独为这次 assignment 请求**指定仓库的 `contents: write`**，并核对返回的仓库、权限（允许附带隐式 `metadata: read`）和 bot 身份。短期令牌只随私密 assignment 字段传输；worker 给单次进程配置 Git credential helper、bot 身份的 `gh` 环境和临时配置目录，不将 token 写入任务结果或 journal。凭据过期后不会为正在运行的进程自动续签。[签发与校验](../../app/github/repositories.py) · [worker 环境测试](../../tests/test_worker_github_environment.py)

因此 worker 可以推分支，却不需要持有 control 用于写 PR 的凭据。首轮工具 `flow.open_pr` 只记录经远端 ref 核验的开 PR 意图；control 收到并接受终态结果后才创建或复用 PR。反过来，GitHub 收到了写请求却没返回响应，本地超时也不能证明远端没有执行：写评论要先按本 App 作者和精确标记找回，不能直接发送第二条。[工具服务](../../app/controler/workflow/tools.py) · [外部写入处理](../../app/controler/workflow/effects.py)

派发时有两种 token：[JobCoordinator.prepare_for_assignment](../../app/controler/workflow/coordinator.py#L138-L180) 收到的 `token` 标识这次 Job 分配，和 worker、连接代次一起用来核对领取权。另一个 token 由 control 按 Flow 保存的 installation ID 和仓库名调用 [Repository.create_push_capability](../../app/github/repositories.py#L250-L276) 换取；返回的 `PushCapability` 才含 GitHub 凭据、有效期及 bot 身份。[协调器](../../app/controler/workflow/coordinator.py#L256-L271)只为首轮和修订任务装配这个私密字段。签发失败且领取仍有效，由 control 将 Job 记为失败；签发期间领取权已变化，则丢弃本次准备结果，不交给旧 worker。

`AGENTFLOW_GITHUB_REQUEST_TIMEOUT_SECONDS` 默认 30，须为正有限数；修改后重启 serve。它限制连接、读、写和连接池等待等单个网络阶段，不限制整轮分页或恢复的总时长。[错误分类](../../app/github/errors.py)区分临时故障与有证据的未发送或拒绝；即使 HTTP 200，GraphQL 写入也可能部分成功且附带 errors。[客户端契约测试](../../tests/test_github_client.py)

## A.6 可选的组织 Projects v2

看板是 Flow 阶段的一个副本。只有配置了 `AGENTFLOW_PROJECTS` 的仓库才同步；每个仓库绑定一个现有组织 Project、一个现有单选字段和五个选项映射。配置写 ID 而非显示名，选项可以复用。示例中的 ID 须替换成真实值，修改后重启 serve。[配置模型与 GitHub 操作](../../app/github/projects.py) · [Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)

```json
{"owner/repo":{"installation_id":42,"project_id":"PVT_xxx","status_field_id":"PVTSSF_xxx",
"options":{"todo":"option1","in_progress":"option2","in_review":"option3","blocked":"option4","done":"option5"}}}
```

[投影查询](../../app/persistence/projects.py)取当前有权更新该 Issue 的 Flow，将 `queued / running / review / blocked / archived` 依次映射到上述五种状态；没有 Issue 的任务不会建卡。[同步器](../../app/controler/reconciliation/projects.py)分别确认「卡已添加」和「Status 已设置」。添加响应丢失时先查远端；无法确认时保留不确定状态，人工删除的已知卡也不会自动重建。限流或看板离线只延后同步，不阻止 Flow 归档和释放 worker。[投影故障测试](../../tests/test_project_sync.py)

`agentflow project check owner/repo` 只读核对安装、看板、字段和选项；`agentflow project status owner/repo` 查看期望版本、确认版本及错误。修复授权或字段后，`agentflow project retry owner/repo` 重新安排核对，不授权重复添加一张结果未知的卡。
