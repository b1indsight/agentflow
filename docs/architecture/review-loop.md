# Review 修订循环：一条评论怎样才算处理完

审查者先问「这里为什么重试三次？」，又在同一条评论里补写「请改成可配置」。如果 worker 已经开始回答第一个问题，什么时候能把讨论标为完成？AgentFlow 让每一轮任务认领**特定版本的反馈**。因此旧答复仍有明确的对象，但不能消费后来补写的修改要求。这个区别贯穿三层：control 记录和分流，worker 在原工作区回答或改码，control 再去 GitHub 核验交付、发布说明。[批次并发测试](../../tests/test_review_batches.py) · [交付与反馈核验](../../app/controler/workflow/revisions.py)

本文讨论已交付 PR 的评审；从 GitHub 观察一次问答和改码的完整步骤，见[在同一张 PR 上提问，再要求改代码](../feature-walkthroughs/review.md)。

## B.1 反馈入口与持久 settle

[事件路由](../../app/controler/events/router.py)识别三种来源：Issue/PR 讨论区评论、review 正文、diff 行上的 inline 评论。它按仓库与编号寻找对应 Flow，过滤本 App 和其他 bot 的反馈。[评审存储](../../app/persistence/reviews.py)以 `(kind, github_id)` 标识评论，对每次有效变化保存版本和所属 attempt（本次执行）。这样不同类别里的同号评论不会串在一起；同一 Issue 上旧 Flow 已见过、此后未改动的评论也不会变成新任务。

审查者可能先发三条行内评论，再提交一份 review。Control 在 PostgreSQL 保存一个合批截止时间（settle），让相近的意见进入同一个 `ReviewBatch`；重启后按原截止时间继续扫描。重复投递、相同内容和过期事件不延长窗口；删除留下删除记录，旧正文不能复活。遇到同秒编辑，先用 GitHub 的 `changes.body.from` 核对前一版；无法排出先后就等[反馈对账](../../app/controler/reconciliation/feedback.py)重新读取，不按网络到达顺序猜。一个 Flow 同时最多一个待判定或正在处理的批次，之后到来的版本保留给下一轮。[合批与重放测试](../../tests/test_review_batches.py)

普通讨论进入 Judge，即使正文写了 `@bot` 或「马上改」。只有 allowlist 中的作者**本人提交** Request Changes，才能在较短的提交窗口后直接进入修订；空的 Request Changes 保留信号，等有可操作的正文或 inline 再开工。编辑不会产生新的强信号，批准、撤销评审也不授权改码。[路由的授权判断](../../app/controler/events/router.py) · [批次选择规则](../../app/domain/review_policy.py)

## B.2 冻结批次与任务协议

对普通讨论，[上下文构建器](../../app/controler/review/context.py)把已选反馈、PR 标题与正文、来源和目标提交、上一轮未决点放入快照。只有 inline 反馈会补读关联线程，沿用评论自带的文件路径和 diff 片段；不会把整个仓库 diff、原 Issue 或其他线程全部送给 Judge。每组最多 60 项、每项最多 4,000 字符、快照最多 96,000 个 JSON 字节。相关线程缺失、被截断或读取期间提交变化时，判断可停在 `wait`；没有现成代码片段不等于不能让 worker 去仓库定位。GitHub 读取报错也不会被保存为「查过、没有内容」。[上下文边界测试](../../tests/test_review_context.py)

[Judge](../../app/controler/review/judge.py)通过独立的 `INFERENCE` Job 判断下一步，不占用 Flow 的编码会话。批次保存上下文及其摘要、推理任务关联和决定；重启可接着取结果。如果尚未执行时发现 PR 的 head/base 已变化，control 丢弃不再适用的上下文和决定，重新观察。下发的 `RevisionTask` 则绑定原 worker、仓库、attempt、batch、会话代次和反馈清单；worker 必须交回这些身份对应的结果。[推理持久化](../../app/persistence/inference.py) · [任务准备](../../app/controler/workflow/revisions.py)

Judge 的结构化输出 `TriageVerdict` 包含 `verdict`、`confidence`、`reason`、`instruction` 和 `open_points`。置信度必须是 `[0,1]` 内的有限数；拒绝额外字段。`answer` 和 `revise` 都需要非空任务指令；Judge 不能填写最终答案，旧记录的 `answer` 字段仅供兼容。`wait`、`ignore` 不得带任务指令，`answer`、`ignore` 不得带未决点。[判定模型](../../app/domain/models.py) · [非法输出测试](../../tests/test_judge.py)

## B.3 判定与反馈消费

| 判断 | 对这一批反馈做什么 |
|---|---|
| `answer` | 把问题交给原工作区的 worker 查证，作为 `purpose=answer` 的 REVISION 执行。Control 负责发布答案。 |
| `revise` | 达到置信度阈值才创建 `purpose=revise` 的 REVISION；混合反馈中需要改码的问题和需要解释的问题一并写进指令。 |
| `wait` 或低置信度 `revise` | 保存原因、未决点和未消费版本；新反馈到来时接续，无新信息不重复作语义判断。 |
| `ignore` | 消费这一批的精确版本，不写 GitHub，也不消费同一评论后来的编辑。 |

「不知道怎么处理」与「推理服务没能返回」有不同的恢复路径。所选证据不全、非法模型输出或不可恢复的配置错误可以留下等待决定；可恢复的推理错误或后端不可用则交回 Job 调度，批次保留待判定。一次推理调用的派发次数与总期限持久化，重启不会刷新这次预算；运行故障不会被发布成对审查者的回复。[Judge 故障分类](../../app/controler/review/judge.py) · [推理任务与重试](../../app/persistence/inference.py)

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

### 回复到哪里，什么时候能解决线程

Control 把核验事实、待发布的说明和处理依据冻结成 `ResultPlan`。讨论区和 review 正文的说明带来源链接发到 PR 讨论区；同一 inline 线程的说明合并，使用**根评论的数据库 ID** 回复。无法唯一定位线程就保留待处理。`addressed` 是 worker 对某版本的声明，消费反馈是 control 的本地决定，`resolved` 则是 GitHub 上整条线程的状态。[说明规划](../../app/controler/review/resolution.py) · [线程写入与核对](../../app/controler/workflow/thread_effects.py)

例如线程有两条人类评论，第一条已有回复并不足以关闭线程。Control 要求必要回复已确认，刷新线程和本地版本，用本次或同一 attempt 的历史核验结果逐条核对作者、正文、时间和身份；线程列表与其评论分别分页，缺页不算完整。执行 resolve 后还会复读。读取与 resolve 之间若又来了新评论，GitHub 无法把两步放进同一事务；系统记录新反馈，不自动反向打开线程。人工点过 resolve 也不能替代消费证据。[完整覆盖测试](../../tests/test_review_resolution.py) · [主动对账](../../app/controler/reconciliation/feedback.py)

回到上面的 JSON：第一条说明发布确认后可消费对应版本；第二条仍未决，因此即使代码已经推送，也不请求复审。空 review 正文可以作为元数据随成功结果消费。只有本轮确有新提交、未决项已清且所需线程处理已确认，才进入复审通知；发送前还要检查后到反馈及 PR 是否仍匹配。[结果计划](../../app/controler/workflow/revisions.py) · [外部写入执行](../../app/controler/workflow/effects.py)

## B.5 取消、重试与恢复

**代码还没交付，先查原会话。** 准备时读取 GitHub 遇到临时错误，有持久化的准备重试窗口；尚未 `Started` 不消耗修订次数。普通修订优先恢复原编码会话，没有会话或能证明恢复请求在执行前就被拒绝时，才允许启动一次替代会话；执行是否发生说不清时保留现场。显式重试失败的 `revise` 更严格：保留原工作区，只记录远端基线，不移动分支，让原会话结合失败状态和人的补充续做；原会话不可用就停止。[准备读取](../../app/controler/workflow/preparation.py) · [执行与恢复](../../app/worker/execution/executor.py)

**代码已核验，看哪一次写入缺回执。** 回复发出后断线，先用本 App 的作者身份与精确 marker 查远端，结果未知时不盲重发。明确的线程权限或删除错误会留下诊断和未消费反馈，不必推翻已核验的代码。其他必须完成的通知若被明确拒绝，显式重试可以续办那项通知，复用已成功回执，不要求 worker 再写一遍代码。[写入处理](../../app/controler/workflow/effects.py) · [恢复测试](../../tests/test_revision_recovery.py)

**取消，看原 worker 是否确认停止。** Control 停用待处理批次和排队任务，向仍在执行的 worker 发持久停止请求；确认前保留绑定。已取消的任务不能靠重放旧结果补发通知。[停止确认](../operations/cancel-drain.md#停止确认) · [取消与结果恢复测试](../../tests/test_revision_recovery.py)

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
