# Review 修订循环：一条评论怎样才算处理完

审查者先问「这里为什么重试三次？」，又在同一条评论里补写「请改成可配置」。如果 worker 已经开始回答第一个问题，control 可以确认 worker 回答过旧问题，但必须把新要求留给下一轮。整个循环围绕一个约定：每轮任务只认领**特定版本的反馈**。本文用「消费」表示 control 已有依据把一个版本从待处理清单中移走；GitHub 上的评论和回复仍然可见。

先认清四个模块，后面遇到「任务成功了，评论怎么还在」就能找到相应的记录：

| 模块 | 手里的输入 | 负责交给下一步的东西 |
|---|---|---|
| 事件路由器、`ReviewStore` | GitHub 事件、Flow 和当前 attempt（一次执行） | 带类别和版本的反馈、合批截止时间，以及到期后认领的 `ReviewBatch`（一批待处理版本）。 |
| 上下文构建器、Judge 和任务协调器 | 冻结的反馈、相关 PR 事实、上轮遗留问题 | 判定和理由。需要回答或改码时，协调器另建 `REVISION` Job，把判定转成给 worker 的指令。 |
| 编码 worker | 绑定原仓库和会话的 `RevisionTask`、本轮允许处理的版本 | 每项处理说明或暂缓原因，连同工作区、提交以及执行过程的证据。 |
| 结果处理器、线程处理器 | 已收件的结果、GitHub 上的 PR 与线程、本地冻结的反馈 | 核验事实，发布说明，确认能消费哪些版本、能否解决整条线程，以及能否请求复审。 |

这四步有不同的完成点。`ReviewStore` 已经记录了一项意见，不代表 Judge 已经读过；Judge 决定 `answer` 不代表审查者已经收到回复；worker 交回成功结果，也不能代替 control 对 GitHub 的复读。下面先跟一条评论走到派发，再看多条意见、并发推送和重启怎样改变预期结果。

本文讨论已交付 PR 的评审；从 GitHub 观察一次问答和改码的完整步骤，见[在同一张 PR 上提问，再要求改代码](../feature-walkthroughs/review.md)。

## 在 Flow 网页追加一句话，为什么会在 PR 上收到回复？

`REVISION` 是接着当前 PR 执行的一轮作业。它有两个入口：GitHub 评审带来一批需要逐项处理的反馈；Flow 网页的「追加任务」则直接提供指令。两者复用原工作区、分支和会话，也复用同一套结果发布规则。

例如，在已有开放 PR 的 Flow 里输入「请说明为什么重试三次」：[网页提交](../../app/controler/web/api.ts)调用 `POST /api/flows/{id}/agent-tasks`，control 先把原文记为当前 Attempt 的用户消息。可以派发新作业时，[`_queue_agent_inputs`](../../app/controler/workflow/service.py#L344-L378)创建 `trigger="strong"`、`feedback=[]` 的批次，把原文放进 `instruction`，直接派 `purpose=revise`，不经 Judge。即使是问句也走这条路径。作业忙时消息先保留；交付状态为 `WAITING` 时则续派 `INITIAL`。[控制台集成测试](../../tests/test_console_api.py#L396-L455)分别检查了活动里的用户原文、worker 收到的指令和空反馈清单。

[worker 提示词](../../app/worker/execution/prompts.py#L120-L149)在两种入口下都把任务指令放在 `instruction (synthesized from review)` 标题下，后面才列出真正的 `Review feedback`。网页原文因此可能看起来像评审意见，但提交网页消息本身不会创建 GitHub 评论。

worker 交回结构化结果，由 control 核验并发布。具体发到哪里，取决于**结果字段和原反馈的位置**：

| 核验通过的内容 | 发布位置 |
|---|---|
| 每轮必填的 `summary` | [`revision_plan`](../../app/controler/workflow/revisions.py#L298-L322)为当前 PR 安排一条普通会话评论。`answer` 加前缀「问题答复」，`revise` 加「修订交付」并注明有无新提交。此处不判断指令来自网页还是 GitHub，所以空反馈、无新提交也会有摘要。 |
| `addressed.note`、`deferred.reason` | [逐项回复规则](../../app/controler/review/resolution.py)把普通评论和 review 正文的处理说明汇总到 PR 会话；行内意见能唯一定位到线程和根评论时，才回复原线程。同线程的多项合为一条，无法定位的保留待处理。 |
| `needs_clarification` | 整批只涉及一个已映射的行内线程时，问题随线程回复；其他情况放在 PR 会话。 |

所以在上面的例子里，PR 上通常出现一条「修订交付（无新提交）：…」，内容是 worker 的总结；网页消息不会因此多出一条行内回复。两类写入在[发布器](../../app/controler/workflow/effects.py#L364-L390)里分别调用 `issue.comment`（以 PR 编号写普通评论）和 `thread.reply`（回复已有行内线程），都不需要提交一份新的 GitHub review。

## B.1 反馈入口与持久 settle

[事件路由](../../app/controler/events/router.py)识别三种来源：Issue/PR 讨论区评论、review 正文、diff 行上的 inline 评论。它按仓库与编号寻找对应 Flow，过滤本 App 和其他 bot 的反馈。[评审存储](../../app/persistence/reviews.py)以 `(kind, github_id)` 标识评论，对每次有效变化保存版本和所属 attempt（本次执行）。这样不同类别里的同号评论不会串在一起；同一 Issue 上旧 Flow 已见过、此后未改动的评论也不会变成新任务。

审查者可能先发三条行内评论，再提交一份 review。Control 在 PostgreSQL 保存一个合批截止时间（settle），让相近的意见进入同一个 `ReviewBatch`；重启后按原截止时间继续扫描。重复投递、相同内容和过期事件不延长窗口；删除留下删除记录，旧正文不能复活。遇到同秒编辑，先用 GitHub 的 `changes.body.from` 核对前一版；无法排出先后就等[反馈对账](../../app/controler/reconciliation/feedback.py)重新读取，不按网络到达顺序猜。一个 Flow 同时最多一个待判定或正在处理的批次，之后到来的版本保留给下一轮。[合批与重放测试](../../tests/test_review_batches.py)

普通讨论进入 Judge，即使正文写了 `@bot` 或「马上改」。只有 allowlist 中的作者**本人提交** Request Changes，[批次策略](../../app/domain/review_policy.py)才允许跳过 Judge，在较短的提交窗口后直接进入修订。Control 为没有正文的 Request Changes 保留信号，等有可操作的正文或 inline 再派任务。编辑不会产生新的强信号，批准、撤销评审也不授权改码。

以 GitHub 评论 501 为例：作者编辑正文后，事件路由器把投递 ID 和 `FeedbackUpdate` 一起交给 [ReviewStore.ingest](../../app/persistence/reviews.py#L48-L105)。投递 ID 用来识别重送；更新里的评论类别、GitHub ID、作者、正文和时间用来确认哪项反馈变了。存储层锁住 Flow，核对当前执行，再将投递记录、反馈版本和需要更新的截止时间一起提交。返回 `True` 表示记录了变化，删除也可能返回 `True`；此时 control 还没有派任务。到期后运行时调用 [ReviewStore.claim_due](../../app/persistence/reviews.py#L265-L324)，得到本轮新认领的批次列表；普通讨论随后从 `pending_triage()` 取出判定。

**时间线：重启、重复投递和真正的编辑。** 假设使用默认 75 秒窗口，第一条普通评论在 12:00:00 入库。服务重启后重新连接同一数据库，12:00:30 扫描仍领不到；12:01:16 再扫描，若没有新的有效输入且 Flow 仍符合条件，可以领到一批，再扫一次不会领到第二批。假如第 60 秒只是重送相同正文，原截止时间不变。由此可以区分「不够安静，仍在收意见」与「没有新变化，已到可认领时刻」。[时钟与重放用例](../../tests/test_review_batches.py#L143-L197)直接推进测试时钟，不需要让测试进程真的等 75 秒。

批次一旦认领，control 不会就地改写清单。同一组测试先认领评论 101 的版本 1（`Add a bound.`），随后收到新正文 `Add an upper and lower bound.`，再收到新评论 102。即使合批窗口已经过去，第一批仍在处理时也领不到第二批。第一批按原清单完成后，下一批恰好含 `(101, 2)`、`(102, 1)`；老批次里仍能读到原句。若排查时只用评论 URL 搜索，两个版本会指向同一个页面，必须连同批次 ID 和本地版本号比对。

空的 Request Changes 是另一条路径。假设有资格的审查者提交 review 却只在行上写了意见，review 正文可以是空串。只收到提交信号时，control 不会派一个没有指令的编码任务；行内意见到达并过了短窗口后，批次策略才生成带实际要求的 REVISION。查看 `test_empty_request_changes_waits_for_inline_then_bypasses_judge` 可以核对：收到 inline 后推进 16 秒，只得到一个 Job，指令包含原意见 `Add a bound`。普通用户在讨论里打出同样的词，仍走 Judge；两种路径由事件、动作和作者决定。

## B.2 冻结批次与任务协议

对普通讨论，[上下文构建器](../../app/controler/review/context.py)把已选反馈、PR 标题与正文、来源和目标提交、上一轮未决点放入快照。只有 inline 反馈会补读关联线程，沿用评论自带的文件路径和 diff 片段；不会把整个仓库 diff、原 Issue 或其他线程全部送给 Judge。每组最多 60 项、每项最多 4,000 字符、快照最多 96,000 个 JSON 字节。相关线程缺失、被截断或读取期间提交变化时，判断可停在 `wait`；没有现成代码片段不等于不能让 worker 去仓库定位。GitHub 读取报错也不会被保存为「查过、没有内容」。[上下文边界测试](../../tests/test_review_context.py)

[Judge](../../app/controler/review/judge.py)通过独立的 `INFERENCE` Job 判断下一步，不占用 Flow 的编码会话。批次保存上下文及其摘要、推理任务关联和决定；重启可接着取结果。如果尚未执行时发现 PR 的 head/base 已变化，control 丢弃不再适用的上下文和决定，重新观察。下发的 `RevisionTask` 则绑定原 worker、仓库、attempt、batch、会话代次和反馈清单；worker 必须交回这些身份对应的结果。[推理持久化](../../app/persistence/inference.py) · [任务准备](../../app/controler/workflow/revisions.py)

Judge 交回的结构化判定 [`TriageVerdict`](../../app/domain/models.py#L395-L429) 包含 `verdict`、`confidence`、`reason`、`instruction` 和 `open_points`。模型拒绝额外字段，要求置信度为 `[0,1]` 内的有限数。`answer` 和 `revise` 都需要非空任务指令；Judge 不能填写最终答案，旧记录的 `answer` 字段仅供兼容。`wait`、`ignore` 不得带任务指令，`answer`、`ignore` 不得带未决点。

**快照里看得到什么？** [相关线程用例](../../tests/test_review_context.py#L110-L156)在一张 PR 里放进巨大的无关补丁、很长的旧 Issue 正文，再挑出 `app.py` 上关于 `+limit = 10` 的行内意见。调用 `build_review_context(repo, flow, batch, 17)` 后，当前反馈仍带路径、片段和原提交 ID；相关讨论只含编号 499，未决点是 `Why ten?`。`files` 为空，`issue`、`task_content` 为 `None`，`ci` 为 `not-fetched`；代码没有请求完整文件列表或 Issue 正文。读到 `not-fetched` 不能报告「检查已通过」，就像没有完整补丁不能报告「PR 没改文件」。

时间边界也适用于同一线程的旁证。冻结时知道评论 500，随后在 GitHub 读线程时看到 499、500、501。如果 501 在截止时间之后发布，上下文可以把 499 作为背景，保留 500 作为本轮输入，却不能绕过截止时间把 501 塞回背景。否则 Judge 会看见 worker 无权消费的意见。[截止时间用例](../../tests/test_review_context.py#L318-L336)分别断言 `feedback` 和 `discussions` 的 ID，检查的正是这个区别。

给 worker 的协议中，`batch_id` 和 `feedback` 共同限定了任务范围；`repo`、`branch`、`pr_number` 和 `base_branch` 指明要回到哪个交付现场；`session_id` 配合 `session_generation` 识别应该接续的会话。`purpose` 则决定可以做哪些动作，取值只有 `answer` 和 `revise`。例如，显式编码重试要传 `retry_status`，如果同时把 `purpose` 填成 `answer`，[任务模型](../../app/contracts/protocol.py#L94-L124)会在校验时拒绝。靠任务标题写「只回答」不能取代协议字段。

### REVISION 的 prompt 在哪里初始化？

控制端先准备数据，worker 再生成文本。派发前，[`prepare_revision`](../../app/controler/workflow/revisions.py#L51-L176)核对当前 Attempt、批次、worker 绑定和开放的 PR，将交付信息、原会话 ID 与代次、批次反馈和 Job 的 `instruction` 装入 `RevisionTask`。首次冻结 `revise` 时，还可把等待派发的网页消息追加到指令末尾。[`freeze_revision`](../../app/persistence/reviews.py#L581-L591)把任务写入 `job.payload["revision"]`；同一 Job 再次准备时复用已冻结的值。

worker 在绑定的工作区里调用 [`build_revision_prompt`](../../app/worker/execution/prompts.py#L94-L160)。普通修订按以下顺序拼接，已有会话和新会话使用同一份文本：

| 段落（出现顺序） | 从哪里来，怎么读 |
|---|---|
| `Original task` → `Delivery` | [原任务](../../app/controler/workflow/preparation.py#L75-L82)取 Flow 保存的正文；Issue 任务没有这份正文时，读取 Issue 当时的标题和正文。交付段列出仓库、PR、源分支、目标分支和 control 观察到的 PR head。 |
| `Previous summary` → `Coding start` → `Unresolved questions` | 分别是当前 Attempt 的上次修订摘要、worker 实际同步到的编码基线、批次遗留问题。普通 `revise` 必须有编码基线，不能拿较早观察到的 PR head 代替；`answer` 只查本地提交，没有 `Coding start`。摘要和遗留问题可以为空。 |
| `Answer/Revise instruction (synthesized from review)` → `Review feedback` | 前者是本轮要做什么，后者是本轮认领了哪些反馈版本。每项带 `item_id`、`version`、`comment_id`、作者和正文；有 diff 片段的行内意见还带文件、原提交和片段。网页单独追加任务时，问题出现在指令段，反馈段显示 `(no inline items)`。 |
| `Rules` → 输出要求 → 可选的 `Frozen PR context` | `answer` 只查证，不改码、不推送；`revise` 在原分支交付，并要求在编码里程碑报告进展。两者都要求返回 `summary`、`vcs_backend_used`、`addressed`、`deferred`、`needs_clarification`。只有任务带评审快照时才追加其 JSON。 |

[执行器](../../app/worker/execution/executor.py#L321-L371)把 prompt、工作目录和 `RevisionCodingOutput` 的 JSON Schema 一起交给编码后端；图片通过执行上下文传入。[Codex](../../app/worker/execution/codex.py#L197-L200)用 `--output-schema` 传文件，[Amp](../../app/worker/execution/amp_agent.py#L455-L457)则把 schema 附在 prompt 末尾。这就是 Amp 消息最后还有一段 `Return exactly one JSON value…` 的原因。普通修订优先 `resume` 原会话；没有会话 ID，或后端明确报告会话不可用且工作区复核通过时，才用同一 prompt `start` 新会话。

**显式编码重试例外。** 有 `retry_status` 时，worker 用[短重试模板](../../app/worker/execution/prompts.py#L108-L117)，只给出上次状态、保留本地和远端工作的要求，以及可选的新指令；不重放原任务、评审快照或编码基线。输出仍受同一 schema 约束。这条路径必须续接原会话，会话缺失或不可用就失败，不另开会话。[重试与会话缺失用例](../../tests/test_revision_delivery.py#L190-L263)

## B.3 判定与反馈消费

| 判断 | 对这一批反馈做什么 |
|---|---|
| `answer` | 把问题交给原工作区的 worker 查证，作为 `purpose=answer` 的 REVISION 执行。Control 负责发布答案。 |
| `revise` | 达到置信度阈值才创建 `purpose=revise` 的 REVISION；混合反馈中需要改码的问题和需要解释的问题一并写进指令。 |
| `wait` 或低置信度 `revise` | 保存原因、未决点和未消费版本；新反馈到来时接续，无新信息不重复作语义判断。 |
| `ignore` | 消费这一批的精确版本，不写 GitHub，也不消费同一评论后来的编辑。 |

先区分「还需要证据」和「推理服务没有返回」。遇到证据不全、非法输出或不可恢复的配置错误，Judge 可以留下等待决定；遇到可恢复的推理错误或后端不可用，Judge 把错误交回 Job 调度，批次保留待判定。推理存储保存每次调用的派发次数与总期限，重启不会刷新预算。Control 不会把这类运行故障当成答案发给审查者。

谁把「请改成可配置」变成编码任务？[JobCoordinator.triage](../../app/controler/workflow/coordinator.py#L79-L135) 先在 Flow 锁内确认批次仍有效并冻结上下文，再在锁外调用 [ReviewJudge.triage(context)](../../app/controler/review/judge.py#L47)。返回的 `JudgeDecision` 带上下文摘要，只能用于这份快照。若判为 `revise` 且达到阈值，协调器先复读 PR，再次加锁确认批次和基线：基线已变就撤销旧上下文；仍匹配才在**同一事务**记录决定、创建含 `batch_id` 和指令的 REVISION Job，并把批次关联到它。推理尚未返回时，[运行时](../../app/controler/runtime.py#L270-L289)先处理其他批次，下轮继续查询。

可将下列判定作为控制样本，假设 PR 仍开放、基线匹配、选中证据完整，阈值取默认值。`instruction` 描述 worker 应去做什么；`reason` 描述选择这条路径的依据，两者不要混写：

```json
{"verdict":"revise","confidence":0.6,"reason":"已约定设置上限，命名尚未确定。",
 "instruction":"为重试次数增加上限。","open_points":["配置项应叫什么？"]}
```

协调器比较的是「小于阈值」，因此 `0.6` 已经够，预期生成 REVISION，批次保留命名问题。只把 `confidence` 改为 `0.599`，同一份输入转入等待，不创建编码 Job，不发布 GitHub 评论，继续扫描也不会为同一份证据反复调用 Judge。[阈值用例](../../tests/test_review_batches.py#L456-L477)检查了两个具体值。若 Judge 填的是 `ignore` 却留下 `open_points`，则属于结构化输出不合法，不能按有效的忽略决定消费反馈。

等待后的下一轮带着前因。例如先存下 `Which limit?`，没有新版本时扫描拿不到批次；作者补充 `Use 10.` 后，新批次包含旧反馈和新答复，并携带之前的未决点。若审查者进一步要求解释，Judge 可以给出 `answer` 及指令 `Explain the configured retry limit.`。这时查看数据库能找到 `purpose=answer` 的 REVISION、批次与 Job 的关联和仍待处理的反馈；查看 GitHub 则还看不到 bot 回复。实际解释须等 worker 查证并通过结果核验。[问答派发用例](../../tests/test_review_batches.py#L561-L583)

## B.4 修订结果

两种 REVISION 都要求 worker 交回 `RevisionCodingOutput`，例如下面这次只处理了第一条意见。`item_id` 与 `version` 指向冻结的本地反馈，`comment_id` 是这项反馈在 GitHub 对应类别里的 ID；正文被编辑后，只有前两者能精确指出 worker 看过哪一版。[输出模型](../../app/domain/models.py) · [结果校验](../../app/controler/workflow/revisions.py)

```json
{
  "summary": "已将最大尝试次数改为配置项，默认仍为三次。",
  "vcs_backend_used": "git",
  "addressed": [{"item_id": 17, "version": 2, "comment_id": 501, "note": "配置项已接入，测试覆盖默认值。"}],
  "deferred": [{"item_id": 18, "version": 1, "comment_id": 502, "reason": "等待确认旧配置的兼容期限。"}],
  "needs_clarification": ["旧配置需要支持到哪个版本？"]
}
```

五个顶层字段都必填，`vcs_backend_used` 为 `git` 或 `jj`；没有待澄清项时传空数组。未上报的意见仍然待处理。未知 ID、重复引用、把同一版同时列为已处理和暂缓，都会让结果校验失败。成功退出和合法 JSON 只是第一道检查；还要看这次任务允许 worker 做什么。[worker 执行器](../../app/worker/execution/executor.py) · [交付契约测试](../../tests/test_revision_delivery.py)

- **回答问题。** Worker 不同步分支，比较会话前后的本地提交（Git HEAD 或 jj 工作提交）；control 复读原开放 PR，要求远端 head 仍等于冻结值。不要求本地提交 ID 与 PR head 相等，不更新交付版本，也不请求复审。两项检查不能发现所有未提交文件变化或其他外部副作用，因此不能把 `answer` 当作只读沙箱。
- **修改代码。** `previous_head_sha` 是上次已交付的提交，`expected_head_sha` 是准备任务时读到的 PR head，`baseline_sha` 才是 worker 实际 fetch 得到的起点。三者可能不同：同事可以在两次读取之间推送。普通修订安全同步到实际起点，保留无法证明已发布的本地工作，不自动 stash 或强推。完成后 worker 检查分支、工作区干净、本地与远端 SHA 一致；control 独立查询原开放 PR、来源和目标分支、远端 ref，验证最终提交等于实际起点或是它的后代。因此同事的提交不会被算作本轮的新修订，合法的无新提交结果也不会触发复审。[VCS 核验](../../app/worker/execution/vcs.py) · [control 的远端核验](../../app/controler/workflow/revisions.py)

用四个符号走一遍最容易混淆的提交比较。[并发推送用例](../../tests/test_revision_recovery.py#L124-L177)让合作者在任务准备前和 fetch 前各更新一次分支，测试用字符串代替实际 SHA：

| 时间点 | 谁看到了什么 | 哪项事实可以据此成立 |
|---|---|---|
| 上次成功交付 | Control 保存 `previous_head_sha = baseline` | 可以追溯上次确认过的交付；现在的远端可能已移动。 |
| 准备修订 | Control 读到 `expected_head_sha = external`，目标分支为 `release` | 可以冻结准备时的 PR 事实；worker 此时还没有 fetch。 |
| Worker 开始 | Worker fetch 得到 `baseline_sha = fetched` | 应用本轮变更时以 `fetched` 为起点，不能把合作者的更新归到 worker 名下。 |
| Worker 结束 | 最终 ref 为 `fetched`，或者为它的后代 `revision` | 前者可以是「合作者已修好」的合法结果；后者在谱系核验通过后才有本轮新提交的证据。 |

同一用例还在结果成功后再应用一次，预期两次都返回 `DONE`。如果两条路径都处理完全部意见、具备请求复审的其他条件，只有最终 ref 为 `revision` 才实际请求 reviewer。换成只回答的任务，control 可以用准备时看到的 `external` 验证 PR 尚未变化，同时让交付记录继续保留原值；发布答案不会把合作者的代码登记为本轮交付。排查「怎么没请求复审」时，先看任务目的和实际 fetch 起点，再看是否还有未决意见。

### 回复到哪里，什么时候能解决线程

Control 把核验事实、待发布的说明和处理依据冻结成 `ResultPlan`。[说明规划器](../../app/controler/review/resolution.py)为讨论区和 review 正文生成带来源链接的 PR 讨论区回复；对于同一 inline 线程，规划器合并说明，指定**根评论的数据库 ID** 作为回复对象。如果无法唯一定位线程，control 保留相关反馈。`addressed` 是 worker 对某版本的声明，消费反馈是 control 的本地决定，`resolved` 则是 GitHub 上整条线程的状态。

写回复和解决线程是两个步骤。[线程处理器](../../app/controler/workflow/thread_effects.py)先确认必要回复，刷新线程和本地版本，再用本次或同一 attempt 的历史核验结果逐条核对作者、正文、时间和身份。线程列表与每个线程里的评论分别分页，缺页不算完整。Control 执行 resolve 后还会复读。读取与 resolve 之间若又来了新评论，GitHub 无法把两步放进同一事务；control 记录新反馈，不自动反向打开线程。

在 `RevisionCodingOutput` 示例中，control 确认已发布评论 501 的说明后，可以消费对应版本。评论 502 仍待确认兼容期限，因此即使代码已经推送，control 也不请求复审。空 review 正文可以作为元数据随成功结果消费。只有本轮确有新提交、未决项已清且所需线程处理已确认，control 才准备复审通知；发送前还要检查后到反馈及 PR 是否仍匹配。

**单项说明怎样影响整条线程？** 假设一条完整线程只有两条人类评论 1、2，以及一条 bot 回复 3。`covered_thread(thread, addressed)` 要求至少找到一条人类评论，并为每一条找到匹配的已处理 inline 版本。若清单含 1、2 的精确版本，覆盖判定为真；漏掉 2、2 的正文后来改过、误传同号 review 正文，或线程只读到第一页且标为不完整，判定均为假。bot 自己的回复不会制造一条新的人工义务。即使 GitHub 把线程显示为 `outdated` 或已经人工解决，漏掉 2 仍然不能通过检查。[线程覆盖用例](../../tests/test_review_resolution.py#L30-L54)覆盖这些反例。通过覆盖检查只证明版本吻合，真正执行和确认 resolve 还需前述步骤。

还可以在 worker 的示例结果上做两个对照。第一，把 `item_id: 17` 留着但改成另一个 `comment_id`，control 无法把说明绑定到原反馈，结果失败；预期不发评论、不请求复审，待处理清单仍含原版本。第二，保留完全有效的 `addressed`，只在 `needs_clarification` 留下问题。此时 control 可以确认代码和所处理的版本，交付 SHA 也可以更新，但批次仍为 `waiting`。待处理版本列表甚至可能已空：等待的是额外澄清，不能只凭「列表长度为零」请求 reviewer。[部分交付与错误引用用例](../../tests/test_revision_delivery.py#L916-L973)分别检查 Job、批次、交付记录和远端写入。

结果处理器将**已收件的 Job** 交给 [verify_revision_result](../../app/controler/workflow/revisions.py#L179-L226)，在数据库事务外读取 GitHub。返回错误文字表示具体哪项核验失败；`None` 表示本步没有报告错误，也可能是计划已冻结或 Job 已过时，不能据此消费评论。GitHub 暂时不可读时，核验器保存下次核验时间并抛出 `ResultPending`；由 [ResultApplication 的恢复循环](../../app/controler/workflow/results.py#L192-L232)续办同一结果。核验返回后，处理器在事务中锁住 Flow 和 Job、按结果摘要保存或复用计划；提交后才按计划向 GitHub 写回复。

## B.5 取消、重试与恢复

**代码还没交付，先查原会话。** 准备时读取 GitHub 遇到临时错误，有持久化的准备重试窗口；尚未 `Started` 不消耗修订次数。普通修订优先恢复原编码会话，没有会话或能证明恢复请求在执行前就被拒绝时，才允许启动一次替代会话；执行是否发生说不清时保留现场。显式重试失败的 `revise` 更严格：保留原工作区，只记录远端基线，不移动分支，让原会话结合失败状态和人的补充续做；原会话不可用就停止。[准备读取](../../app/controler/workflow/preparation.py) · [执行与恢复](../../app/worker/execution/executor.py)

**代码已核验，看哪一次写入缺回执。** 回复发出后断线，先用本 App 的作者身份与精确 marker 查远端，结果未知时不盲重发。明确的线程权限或删除错误会留下诊断和未消费反馈，不必推翻已核验的代码。其他必须完成的通知若被明确拒绝，显式重试可以续办那项通知，复用已成功回执，不要求 worker 再写一遍代码。[写入处理](../../app/controler/workflow/effects.py) · [恢复测试](../../tests/test_revision_recovery.py)

**取消，看原 worker 是否确认停止。** Control 停用待处理批次和排队任务，向仍在执行的 worker 发持久停止请求；确认前保留绑定。已取消的任务不能靠重放旧结果补发通知。[停止确认](../operations/cancel-drain.md#停止确认) · [取消与结果恢复测试](../../tests/test_revision_recovery.py)

**为什么「重试」要区分明确拒绝和结果未知？** 设想结果计划的最后一步是请求 reviewer，GitHub 明确返回 422，客户端能确认请求被拒绝。恢复用例要求 Flow 进入阻塞，保留已经核验的修订、写入记录和错误；服务重启也不能凭空把这一步确认成功。修复 reviewer 配置并显式重试时，处理器可以针对明确失败的步骤续办。反之，如果发送说明后连接断开，系统连评论是否已经出现都不知道，记录的写入状态是 `UNCERTAIN`。此时显式重试 Flow 也不会把那次写入变回「从未尝试」；应先找远端回执，以免同一线程收到两条说明。[未知写入用例](../../tests/test_revision_recovery.py#L589-L608)同时检查了写入状态和结果尚未应用的事实。

## B.6 参数与验证

以下是[服务端配置](../../app/controler/config.py)和[worker 配置](../../app/worker/config.py)的默认值。窗口、预算、空闲超时解决的是不同问题，调大其中一个不会给另外两项续期。

| 参数 | 默认值及边界 |
|---|---|
| `AGENTFLOW_SETTLE_SECONDS` / `AGENTFLOW_SUBMIT_GRACE_SECONDS` | 75 / 15 秒。普通讨论的静默窗口，以及提交 review 后留给同批意见的短窗口。 |
| `AGENTFLOW_WORKER_SWEEP_INTERVAL` | 5 秒。周期扫描已持久化的截止时间，到点不代表模型已开工。 |
| `AGENTFLOW_CONFIDENCE_THRESHOLD` / `AGENTFLOW_MAX_REVISION_CYCLES` | 0.6 / 20。前者用于 Judge 判 `revise`；后者按 attempt 计数，在 REVISION `Started` 时计入，新建重试任务并开始执行才再计一次。 |
| `AGENTFLOW_JUDGE_MODEL` / `AGENTFLOW_JUDGE_API_KEY` / `AGENTFLOW_JUDGE_API_BASE` | 默认不配置；模型、凭据和可选的 API 地址供 control 托管的 bub 推理 worker 使用。没有合适的 bub 时，可由具备推理能力的普通 worker 在独立会话处理。[推理调度](../../app/domain/inference_policy.py) |
| `AGENTFLOW_JUDGE_TIMEOUT_SECONDS` | 60 秒。一次推理调用的总预算，含排队、最多三次授权派发、通信和退避。 |
| `AGENTFLOW_CODEX_RUN_TIMEOUT_SECONDS` / `AGENTFLOW_AMP_RUN_TIMEOUT_SECONDS` | 均为 1,200 秒。编码进程连续无新 stdout/stderr 输出的空闲时限；有进度输出的长任务可超过该时长。 |

核对一次「修订次数多了一次」时，先按 attempt 找 `Started`，不要数评论、批次或响应包。[计数原子性用例](../../tests/test_revision_recovery.py#L275-L286)在事务里接受 `Started` 后模拟崩溃，回滚后次数为 0，Job 的 `started_at` 也为空。再接受同一任务的开始消息才记 1；相同消息重复到达时返回未再次启动，次数仍是 1。这能区分三种现场：准备阶段重试消耗的是准备窗口，编码真正开始消耗修订次数，核验或通知恢复则继续处理既有结果。

观察一次评审可以依次记下：**评论类别、ID 和版本 → 批次与截止时间 → 判定与 Job 的 purpose → 实际基线和结果 → 说明及线程回执**。例如 PR 上暂时没有回复：若批次还在等安静窗口，先核对截止时间；若在等澄清，找 `open_points`；若 worker 已经完成，查看结果计划和哪一项远端写入缺回执。三种现场的 GitHub 页面可能一样，但只有最后一种需要核实写入，前两种重新派编码任务都无助于完成当前讨论。
