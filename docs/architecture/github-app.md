# GitHub App 接入：谁收事件，谁能改仓库

给 Issue 加上 `flow` 标签后，GitHub 把一条事件投递给 control。要看懂后续动作，先把一次投递、一次 Flow 和一次安装分开：投递是 GitHub 发来的消息；Flow 是 control 围绕 Issue 安排的工作；安装实例（installation）是仓库所有者授予 App 的访问范围。一次成功投递不能替代另外两项授权。

| 沿着一次加标签的动作读代码 | 模块回答的问题 | 能留下什么证据 |
|---|---|---|
| HTTP 入口和收件箱 | 发送方是否持有 Webhook secret？以前收过这个 delivery ID 吗？ | 原始请求体的摘要、事件名、投递处理状态。此时 GitHub 还没有给 worker 任务。 |
| 事件路由器和 Flow 存储 | 谁在什么仓库的哪张 Issue 上加了哪个标签？发起人有权触发工作吗？ | 符合规则才创建或找到活动 Flow。签名正确的无关事件也可以正常完成收件处理。 |
| GitHub 资源客户端和任务协调器 | 这次安装能操作哪个仓库？本次 assignment 需要哪项权限？ | Control 绑定安装和目标资源，给执行编码的 worker 准备一次受限的推送凭据。 |
| Worker 与结果处理器 | 分支是否已送到远端？谁把交付结果写回 PR？ | Worker 提交推送证据；control 核对远端，按结果执行创建 PR、回复或请求复审。 |

排查时也按这条分工找：GitHub 显示投递失败，先看 A.4；投递成功却未启动工作，先看 A.2、A.3；任务开始准备却推不了分支，查 A.1、A.5。组织看板只反映 Flow 阶段，见 A.6；看板卡片不是任务的启动凭据。

## A.1 认证链

Control 使用 `AGENTFLOW_GITHUB_APP_ID` 和 `AGENTFLOW_GITHUB_PRIVATE_KEY_PATH` 初始化 [GitHubApp](../../app/github/apps.py)，要求 ID 为正数、私钥可读且非空。SDK 用私钥签短期 [App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)。Control 以 App 身份读取自身的 slug，得到 `slug[bot]`，并可换取[安装令牌](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)。安装令牌通常一小时过期，权限不能超过该 installation 已获授权的仓库与操作。

对于标签触发的 Flow，[路由器](../../app/controler/events/router.py)从事件取 `installation.id`，和仓库、Issue 一起交给 control。Control 用安装 ID 选择请求身份，用仓库名和编号绑定操作对象。绑定资源时还没联网；持有一个 PR 编号也不等于知道它是否仍开放。[PR 资源](../../app/github/pull_requests.py)将已读取的事实与未读取的字段区分开；交付前仍需复读 PR 和分支。

调用方用 `Repository(installation, full_name)` 或 `Project(installation, config)` 本地绑定资源。需要最新默认分支或归档状态时，调用 [`await fetch_repository(installation, full_name)`](../../app/github/repositories.py#L156-L164) 取得新的 `Repository`。只查分支的 SHA 时，直接调用 [`await fetch_ref(installation, full_name, branch)`](../../app/github/repositories.py#L180-L189)，无需先读取仓库信息。两个函数均位于 `app.github.repositories`。

知道编号时，用 `await Issue(repo, number).refetch()` 或 `await PullRequest(repo, number).refetch()` 读取当前状态；只有分支名时，用 [`find_pull_request_by_head(repo, branch)`](../../app/github/pull_requests.py#L266-L278) 查找 PR。读取审核线程时，把 PR 传给 [`fetch_threads(pr, cursor=...)` 或 `list_review_threads(pr)`](../../app/github/threads.py#L110-L145)：前者读一页，后者在请求和字节预算内遍历线程及评论。

以安装 42 下的 `octo/repo` 为例，调用方通过安装实例为指定仓库申请推送凭据：

```python
installation = github.installation(42)
capability = await installation.create_push_capability("octo/repo")
assert capability.repository == "octo/repo"
```

在[客户端契约用例](../../tests/test_github_client.py#L427-L482)中，请求参数明确指定安装 `42`、仓库列表 `["repo"]` 和权限 `{"contents": "write"}`。成功时返回对象带有效期和经过校验的 bot 用户 ID；失败时抛出错误，调用方不能用本机个人账号顶替。断点检查仓库名、权限和有效期就足够，避免打印 token。

读 PR 时也要区分「认识对象」和「知道状态」。历史回执可以只保存 PR 编号、分支、状态和 head SHA，没有 `merged_at`。用 `PullRequest.model_validate` 恢复短回执时，读取缺失的 `merged_at` 会抛出 `AttributeError`；如果输入明确写了 `"merged_at": null`，同一属性才返回 `None`。[短回执用例](../../tests/test_github_client.py#L361-L380)同时检查两种输入。把缺字段一律补成空值会凭空制造一次观察，也无法回答「从上次交付到现在是否已合并」。

## A.2 注册与最小权限

将 App 安装到需要处理的仓库，按[部署配置](../deployment/README.md)设置公开 HTTPS 地址 `/webhook`、JSON payload 和与 `AGENTFLOW_GITHUB_WEBHOOK_SECRET` 相同的 secret。在 [GitHub 事件订阅](https://docs.github.com/en/webhooks/webhook-events-and-payloads)中选择 **Issues、Issue comment、Pull request、Pull request review、Pull request review comment**。如果用专门的仓库分发 skills，再授权该仓库并订阅 **Push**：配置的默认分支更新后，control 通知执行 worker 刷新，已有 Flow 保留原快照。

| 授权范围 | 谁需要，用来做什么 |
|---|---|
| Repository Metadata: read；Contents: read/write | 识别仓库、读分支与提交、通过 HTTP Git 推送。下发给 worker 的权限另行收窄。 |
| Issues: read/write | Control 读取 Issue、管理标签、回写讨论。 |
| Pull requests: read/write | Control 创建或复用 PR，读取评审，回复或解决线程，请求 reviewer。 |
| Checks: read；Commit statuses: read；Actions: read | 控制台读取检查与提交状态；Actions 权限用于按 check suite 补全工作流名称。名称查询失败仍显示已读到的检查，附缺失原因。 |
| Organization Projects: read/write（可选） | 将 Issue 卡片的 Status 同步到已配置的组织看板。 |

安装方需要批准新增的 [GitHub 权限](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)。Control 管理者另行维护两份名单：`AGENTFLOW_ALLOWLIST` 决定谁能通过标签启动任务，也决定谁提交的 Request Changes 可以直接进入[修订循环](review-loop.md)；`AGENTFLOW_REVIEWERS` 决定交付后向谁请求评审，留空就不请求。调整任一名单都不会扩大安装权限。

部署时逐项核对入口和出口，可以避免把不同的问题都归因于「缺权限」：

1. **GitHub 是否应该发消息？** 只给 App 授予 Issues 写权限，并不会订阅 Issues 事件。对照 App 的事件订阅和目标仓库的安装范围，然后到 GitHub 投递记录找这张 Issue 的 `issues` 事件。没有投递时，调整 worker 的 Git 配置不会产生事件。
2. **Control 是否应该接受这个人的操作？** 假设 `alice` 在 allowlist 内，`bob` 仅在 reviewers 内。两个人加相同标签时，入口依据相同的 secret 验签；路由时只有 `alice` 的加标签操作满足发起人条件。若想请 `bob` 检查交付，检查 reviewers；不必为了请求评审顺带给 `bob` 启动任务的资格。
3. **安装能否完成请求？** 即使 `alice` 符合触发条件，GitHub 仍会检查安装实例的仓库和权限。提高发起人权限不会修复 App 无法读仓库的问题。同理，仅为看板补组织权限不会改变 worker 拿到的 `contents` 范围。分别向仓库安装方和 control 管理者核实各自掌握的配置。

## A.3 仓库标签：建好定义和触发任务是两件事

`AGENTFLOW_TRIGGER_LABEL`、`AGENTFLOW_IN_REVIEW_LABEL`、`AGENTFLOW_DONE_LABEL` 默认分别是 `flow`、`flow:in-review`、`flow:done`；三个名字不能为空，也不能忽略大小写后重名。[后台扫描器](../../app/controler/reconciliation/labels.py)约每 300 秒枚举有效安装及非归档仓库，完整读取标签后补缺，不改已有颜色、描述，也不代人给 Issue 加标签。确认齐全后每 24 小时复查，重启后重新确认。限流会延后扫描；`/readyz` 正常不保证标签已建好。[标签核对测试](../../tests/test_github_labels.py)

真正启动任务的是白名单用户的 `labeled` 事件；移除触发标签会取消该 Issue 上活动的 Flow。系统回写评审、完成状态时只增删对应标签，保留用户的其他标签。[事件路由](../../app/controler/events/router.py) · [标签操作](../../app/github/issues.py)

**一个容易误判的空结果。** [标签分页用例](../../tests/test_repository_labels.py#L62-L93)构造了 100 个用户标签，再放入已有的 `Flow`，颜色是白色、描述是 `user text`。扫描器要保证 `flow` 和 `custom:review` 存在。虽然第一页里没有 `flow`，扫描器会读下一页，按忽略大小写的名字认出 `Flow`。`await repo.ensure_labels(definitions)` 首次返回 `("custom:review",)`，表示只创建了这个定义；远端列表包含两个定义后，再调用返回 `()`。两次共发送一次创建请求，已有标签保留原貌。

若列标签时 GitHub 返回 404，同一用例要求抛出错误，创建请求次数仍为一。原因很具体：服务没读到列表，无法区分「仓库没有该标签」和「安装看不到该仓库」。还有一种竞争情形：完整读取之后、创建之前，维护者已经建好了标签。创建返回 422 时，客户端另读指定名字；能确认同名定义才接受现状，不能单凭 422 判成功。以上任何一步都没有给某张 Issue 贴标签，观察仓库标签列表无法推断某张 Issue 启动过 Flow。

## A.4 Webhook 校验

[HTTP 入口](../../app/controler/api.py)对**原始字节**计算 HMAC-SHA256，用 `compare_digest` 比较 `X-Hub-Signature-256`。先限体积、再验签，最后检查 delivery ID、事件名和 JSON 对象。超长返回 413，签名错返回 401，非法格式返回 400。相同投递可去重，同一 ID 的内容冲突返回 409，收件数据库不可用返回 503。[GitHub 验签规范](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

**202 只确认收件。** 收件箱先持久化投递，消费者再领取，在同一数据库事务里路由事件、记录控制台观察、完成收件记录；事务提交后才唤醒 worker。进程中途崩溃，下一次领取可以接手，旧领取者不能确认新的领取。消费者也会过滤 bot 自己发表的评论，即使验签成功也不会再次启动评审。运维操作见[查看与重试投递](../operations/webhook-inbox.md)。

例如，给 `owner/repo#7` 加标签后，若 GitHub 再次投递同一事件，可以沿两个调用点判断该由谁重试：

1. [HTTP 入口](../../app/controler/api.py#L244-L253)经 [ServerRuntime.receive_webhook](../../app/controler/runtime.py#L300-L302) 调用 [WebhookStore.receive](../../app/persistence/webhooks.py#L38-L61)，传的是 **delivery ID**、事件类型、解析后的对象和**原始字节**的 SHA-256。正常返回没有 Flow ID：新收件已提交，或同一投递早已收下。重复请求不会重置处理进度。
2. [WebhookConsumer.sweep](../../app/controler/events/webhooks.py#L53-L109) 调用 [WebhookStore.claim](../../app/persistence/webhooks.py#L63-L95)，拿到带领取凭据的 `Webhook`；`None` 表示此刻没有可领的投递。消费者在业务事务中用 `lock(item)` 核验领取权，再调用 `EventRouter.dispatch`；`complete(item)` 成功才能连同业务修改提交。处理失败由消费者调用 [WebhookStore.fail](../../app/persistence/webhooks.py#L97-L107) 安排退避或标记 `blocked`。已提交后即使唤醒失败，也由周期扫描接手，无须重新加标签。

入口保存原始摘要有一个具体后果：`{"action":"labeled"}` 与 `{ "action": "labeled" }` 解析后字段相同，签名和摘要的输入字节却不同。假定两次请求各自通过验签，事件名和 delivery ID 相同，第一份已经收件，第二份会遇到内容冲突。原样重送才是重复投递。代理转发请求时若先解码再生成 JSON，即使只多一个空格，也可能在摘要检查之前就因签名不匹配收到 401。

如果 GitHub 已显示 202，可以用收件箱状态区分下一步：

| 观察到的顺序 | 预期结果和原因 |
|---|---|
| 同一个符合触发条件的加标签事件连续送两次，消费者尚未扫描 | 只有一条 `pending`；第一次扫描处理 1 条，创建该 Issue 的活动 Flow。处理完再原样送一次，扫描处理 0 条，Flow ID 不变。 |
| 消费者领取记录后停顿，领取到期后才恢复 | 旧领取者不能靠原来的凭据确认完成。后续扫描重新领取、核验并完成，避免旧进程覆盖新处理结果。 |
| 同仓库先到的事件反复处理失败，另一仓库也有事件 | 出错仓库的后续投递等待，另一仓库仍可推进。应修复先到投递的错误，再重试那一条；先处理后到的取消或加标签可能颠倒人的操作。 |

[收件与重放测试](../../tests/test_webhook_inbox.py#L78-L89)断言重复投递前后的 Flow ID；同文件的 `test_poison_delivery_does_not_block_following_event_and_can_retry` 演示两个仓库的区别：故障注入期间第一条达到十次尝试并阻塞，同仓库第二条仍为 `pending`，另一仓库已经 `done`。修复故障并重试第一条后，一次扫描能完成剩下两条。这里的重试对象是 delivery ID，重新给 Issue 加标签会制造另一个事件。

## A.5 密钥管理

App 私钥和验签 secret 留在 control。临近下发编码任务时，control 单独为这次 assignment 请求**指定仓库的 `contents: write`**，并核对返回的仓库、权限（允许附带隐式 `metadata: read`）和 bot 身份。短期令牌只随私密 assignment 字段传输；worker 给单次进程配置 Git credential helper、bot 身份的 `gh` 环境和临时配置目录，不将 token 写入任务结果或 journal。凭据过期后不会为正在运行的进程自动续签。[签发与校验](../../app/github/apps.py#L159-L183) · [worker 环境测试](../../tests/test_worker_github_environment.py)

因此 worker 可以推分支，却不需要持有 control 用于写 PR 的凭据。首轮工具 `flow.open_pr` 只记录经远端 ref 核验的开 PR 意图；control 收到并接受终态结果后才创建或复用 PR。反过来，GitHub 收到了写请求却没返回响应，本地超时也不能证明远端没有执行：写评论要先按本 App 作者和精确标记找回，不能直接发送第二条。[工具服务](../../app/controler/workflow/tools.py) · [外部写入处理](../../app/controler/workflow/effects.py)

派发时有两种 token：[JobCoordinator.prepare_for_assignment](../../app/controler/workflow/coordinator.py#L138-L180) 收到的 `token` 标识这次 Job 分配，和 worker、连接代次一起用来核对领取权。另一个 token 由 control 按 Flow 保存的 installation ID 和仓库名调用 [Installation.create_push_capability](../../app/github/apps.py#L159-L183) 换取；返回的 `PushCapability` 才含 GitHub 凭据、有效期及 bot 身份。[协调器](../../app/controler/workflow/coordinator.py#L256-L271)只为首轮和修订任务装配这个私密字段。签发失败且领取仍有效，由 control 将 Job 记为失败；签发期间领取权已变化，则丢弃本次准备结果，不交给旧 worker。

`AGENTFLOW_GITHUB_REQUEST_TIMEOUT_SECONDS` 默认 30，须为正有限数；修改后重启 serve。它限制连接、读、写和连接池等待等单个网络阶段，不限制整轮分页或恢复的总时长。[错误分类](../../app/github/errors.py)区分临时故障与有证据的未发送或拒绝；即使 HTTP 200，GraphQL 写入也可能部分成功且附带 errors。[客户端契约测试](../../tests/test_github_client.py)

**有个人 Git 配置的主机。** [凭据环境用例](../../tests/test_worker_github_environment.py#L26-L88)先配置一个能返回个人用户名的全局 credential helper，再在带 assignment 的子进程环境中对目标 URL 执行 `git credential fill`。Git 应返回用户名 `x-access-token` 和本轮凭据；个人名和个人密码均不出现。测试使用假 secret，并捕获输出做断言。在真实主机上，该命令会返回可用密码，不要把命令输出贴进排障记录。

同组用例还覆盖会话恢复：即使父进程已经登录个人 `gh`，新启动和恢复的编码进程都应看到本轮的 `GH_TOKEN`、目标 `GH_REPO` 和独立的 `GH_CONFIG_DIR`。测试让子进程误把假 secret 写进结果，期望读到的结果显示 `[REDACTED]`，临时配置目录也已清理。这里验证的是传递和清理凭据的边界。worker 能用安装身份访问指定仓库，不代表某个 GitHub 写操作一定属于本次 Flow；control 仍需独立核实远端 ref、PR 和本地计划。

## A.6 可选的组织 Projects v2

看板是 Flow 阶段的一个副本。只有配置了 `AGENTFLOW_PROJECTS` 的仓库才同步；每个仓库绑定一个现有组织 Project、一个现有单选字段和五个选项映射。配置写 ID 而非显示名，选项可以复用。示例中的 ID 须替换成真实值，修改后重启 serve。[配置模型与 GitHub 操作](../../app/github/projects.py) · [Projects API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects)

```json
{"owner/repo":{"installation_id":42,"project_id":"PVT_xxx","status_field_id":"PVTSSF_xxx",
"options":{"todo":"option1","in_progress":"option2","in_review":"option3","blocked":"option4","done":"option5"}}}
```

[投影查询](../../app/persistence/projects.py)取当前有权更新该 Issue 的 Flow，将 `queued / running / review / blocked / archived` 依次映射到上述五种状态；没有 Issue 的任务不会建卡。[同步器](../../app/controler/reconciliation/projects.py)分别确认「卡已添加」和「Status 已设置」。添加响应丢失时先查远端；无法确认时保留不确定状态，人工删除的已知卡也不会自动重建。限流或看板离线只延后同步，不阻止 Flow 归档和释放 worker。[投影故障测试](../../tests/test_project_sync.py)

`agentflow project check owner/repo` 只读核对安装、看板、字段和选项；`agentflow project status owner/repo` 查看期望版本、确认版本及错误。修复授权或字段后，`agentflow project retry owner/repo` 重新安排核对，不授权重复添加一张结果未知的卡。

可以把期望版本理解为「根据当前 Flow，现在应该展示什么」，把确认版本理解为「control 在 GitHub 上已经核实到哪一次变化」。例如同步器正在把卡片改为 `in_review`，用户此时取消 Flow，期望版本转为 `done`。旧请求即使成功返回，也只证明较早的一次写入；同步器不能拿旧响应确认新版本。下一轮重新核对并设置 `done` 后，两边才能追平。[并发写入用例](../../tests/test_project_sync.py#L404-L440)同时覆盖了在读取期间发生变化的情形：在写入前已发现期望过时，连那次 `in_review` 都不应发送。

另一个常见现场是服务重启后看到版本落后。若添加卡片已经生效、只是响应丢失，恢复扫描先找现有卡和选项，预期仍只有一次 `add`，状态写也无需重复。若管理员明确删除或归档了已知卡，重试后记录仍可保持阻塞，避免自动推翻人的决定。同一张 Issue 取消旧 Flow 后再启动新 Flow，则由新 Flow 接管既有投影，期望回到 `todo`，不额外添加卡片。这三个场景分别涉及回执、人工操作和归属切换；判断是否要干预之前，先比对 Issue、当前 Flow 和投影版本，再看页面上的 Status 文案。[恢复与接管用例](../../tests/test_project_sync.py#L323-L359)
