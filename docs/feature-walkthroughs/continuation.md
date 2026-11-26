# 续办任务：追加、重试和重新开工分别保留什么？

生命周期模块管理一项需求（Flow）下的每一轮执行（Attempt）：它把补充要求记在当前轮，把符合条件的失败作业交给 worker 重试，并在结论明确时归档。只有重新开工才创建下一轮 Attempt。值班交接时请分别记下 Flow ID、当前 Attempt ID、关联 PR 和最后一条活动。Flow ID 能找回需求；判断操作还能不能作用于你刚看过的那轮工作，要用 Attempt ID。

## 先选动作，再决定往哪里写

| 接手时知道的事实 | 选择 | 执行后先核对什么 |
| --- | --- | --- |
| 代码还在评审，产品方补了一条可验收的要求 | 在当前 Flow 追加任务 | 活动里能看到要求，归属当前 Attempt；不一定马上有新作业 |
| 原目标和输入都没变，编码作业因可恢复的故障失败 | 排除故障后，在当前 Flow 重试 | 作业可以换，Attempt ID 保持原值 |
| PR 已经合并，或无 PR 的工作已经有经确认的结论 | 查看或申请归档 | 记录本轮的原因与结论；另查停止与释放情况 |
| 旧轮已归档且安全释放，需要在同一需求下再做一轮 | 从归档详情重新开工 | Flow ID 不变，Attempt ID 改变，新工作从排队开始 |

例如，昨天的作业失败在读取仓库，今天权限已恢复，需求仍然是同一份 CSV：先修权限，再用重试。如果同事同时带来新要求「在帮助文本里注明导出日期按哪个时区计算」，请把要求作为输入明确登记。重试请求没有供你填写新正文的字段，无法让编码工具凭空知道这处改动。反过来，已经合并的旧 PR 也不能用追加的方式再接收下一版代码。先确认是否真的要开始新一轮，再提交写操作。

## 工作没结束：补一条要求

例如评审中才发现导出的日期还需要显示时区。用控制台的追加任务入口写清楚要求，可附图；已有 PR 时，新要求会进入这次开工的修订流程。详情先显示输入已登记，等允许派发时才会交给编码工具。它不会把用户刚输入的字直接插进一个正在运行的命令。

通过 API 提交时，同一个请求 ID 和同样的输入重试，只应留下一条记录；沿用同一个 ID 却改了内容，会收到冲突响应。这个区别在网络超时后尤其重要：重试原请求和提出第二项新要求应当能区分。见[追加只记录一次](../../tests/test_console_api.py#L396)和[不能立即执行时先保存](../../tests/test_console_api.py#L473)。若原 Job 执行失败而目标不变，使用重试继续当前 Attempt，无需创建新任务。

追加时，[`ControlService.append_agent_task`](../../app/controler/workflow/service.py#L309-L334)在 Flow 锁内比较旧请求、保存新消息，接着询问 [`_queue_agent_inputs`](../../app/controler/workflow/service.py#L344-L378)能否安排工作。如果本轮还有排队或运行中的作业，它返回 `False`，已记录的消息仍保留。已有打开的 PR 且可以派发时，它按顺序取出本轮消息、建评审批次，把 `purpose=revise` 的作业与批次关联后返回 `True`，提交后才唤醒调度。

### 实操：验证一次超时重送只留下一个要求

在有开放 PR 的测试 Flow 上练习。先以所有者身份登录控制台并打开详情，核对仓库和 PR；下面在该控制台网页的开发者工具中执行，先把 `123` 换成详情里的 Flow ID。代码会真实追加一条任务。为了模拟「服务已经写入，客户端却没收到确认」，它故意把完全相同的请求发两次。不要在生产任务上为了试验随意追加指令。

```javascript
const flowId = 123;
const path = `/api/flows/${flowId}`;
const message = {
  instruction: "请在 CSV 导出的帮助文本中注明日期使用的时区，保留现有导出格式。",
  request_id: crypto.randomUUID(),
};
async function appendOnce(body) {
  const r = await fetch(`${path}/agent-tasks`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const detail = await r.json();
  console.log(r.status, detail.detail ?? { attempt: detail.current_attempt_id, phase: detail.phase });
  return r.status;
}
if (await appendOnce(message) === 200) {
  await appendOnce(message);
  await appendOnce({ ...message, instruction: "另一个目标：改动导出列顺序。" });
}
```

在测试前提成立时，前两次都返回 `200`，第三次返回 `409 agent_task_request_conflict`。第三次沿用了前两次的 UUID 却换了内容；这项被拒绝的要求不应出现在工作区。两个成功响应之间调度可能已经运行，阶段字段可以不同。请在活动中按作者和原文找到第一次登记的消息，核对它只出现一次、属于刚才记下的 Attempt，再去原 PR 核对帮助文本。测试也检查了作业指向原分支及原会话，且只在事务提交后通知 worker 一次。[重复请求、冲突和最终修订的断言](../../tests/test_console_api.py#L396-L465)

实际遇到网络中断时，先查活动；需要重送就沿用第一次保存的 UUID、正文及附件。示例所在页面仍开着时可单独调用 `appendOnce(message)`，重新执行整段却会生成另一个 UUID，相当于提出新要求。若原任务已在运行，允许系统先登记输入：本轮没有安全的派发时机时，后台保留这条消息，待条件满足再取走。若第一次请求确实到了，之后的重复响应只说明登记成功；它不会承诺工具此刻正在处理。若当前是只读历史记录，写入口会拒绝，不要把 `409 flow_read_only` 当成网络故障重送。[只读历史的四种拒绝](../../tests/test_console_api.py#L200-L215)

## 作业失败、目标没变：在本轮重试

点「重试」时不带新正文：[API 调用 `ControlService.retry(flow)`](../../app/controler/console/api.py#L309-L338)。例如首次编码失败后，服务会在[当前 Attempt 内创建替代作业](../../app/controler/workflow/service.py#L480-L512)，带上失败作业的输入，再通知调度。[连点两次的测试](../../tests/test_console_api.py#L907-L926)检查了第一次返回 `queued`、Attempt ID 不变，第二次因已在排队而收到 `409 retry_no_change`，没有第三个作业。若已归档，仅有符合条件的归档通知可补做，否则收到 `retry_archived`；两者都不会开始新 Attempt。[归档后的重试分支](../../app/controler/workflow/service.py#L405-L433)

## 合并后：先看结论，再看能否重新开工

在 GitHub 合并当前 PR。控制端确认合并后，详情应显示 `archived`，原因为 `succeeded`；该任务从活跃列表移出。即使原 worker 当时离线，这个已经发生的合并也不会撤销。反过来，归档结论不能证明旧进程已停止：未确认安全释放时请求重新开工，服务会拒绝（HTTP 409）。[合并但清理尚未完成的测试](../../tests/test_console_api.py#L744)同时检查了详情、活跃列表和拒绝结果。

[收到合并事件的 `on_merged`](../../app/controler/workflow/service.py#L263-L300)在锁内校验仓库、安装、PR 归属和投递 ID，再把 GitHub 的合并时间和提交交给 [`record_merge`](../../app/controler/workflow/lifecycle.py#L280-L303)。对当前一轮，它在同一事务里保存事实、发起停止、撤下未执行作业、记录通知和归档结论；提交后才通知后台。若事件属于旧 Attempt，则只更新历史。因此不必等 worker 在线才能确认合并，旧 PR 的迟到事件也不会结束新一轮。

没有 PR 的工作也可以由所有者结束，记录成功或放弃及相应说明；这不会制造一次 GitHub 合并。若原执行还需停止，先完成收尾。不要把“重试”当作撤销归档：它不能重新打开一个已归档 Flow。

[归档按钮](../../app/controler/console/api.py#L266-L281)把页面看到的 Attempt ID、原因和结论交给 [`ControlService.finish`](../../app/controler/workflow/service.py#L141-L186)。手动选择成功时，无 PR 却没写结论，或已有 PR 但尚未合并，服务都会返回 `archive_rejected` 并保留原状态。放弃本轮不要求先合并 PR。可以结束时，服务写下结论并尝试收尾，接口重新读取详情。在[放弃运行中任务的测试](../../tests/test_console_api.py#L853-L889)里，响应已经是 `archived`，但停止请求仍待 worker 确认、工作区尚未释放。

## 有了新目标：在同一个 Flow 下重新开工

当旧绑定等收尾条件已满足，从归档详情重新开工。服务核对你看到的 Attempt 仍是当前这一轮；过时的页面不能替新的 Attempt 再开一轮。成功后比较前后的详情：Flow ID 相同，Attempt ID 已改变，任务回到排队；旧的归档说明仍属于原 Attempt，新工作不会接管原来的 PR。[归档与重新开工测试](../../tests/test_console_api.py#L765)也检查重复归档、误用重试和过期请求。

这里的 [`resume` 接口](../../app/controler/console/api.py#L285-L306)等的是控制端的事务。[`ControlService.resume(flow, expected_attempt_id=…)`](../../app/controler/workflow/service.py#L189-L227)重新锁定 Issue 和 Flow，检查归档记录、原执行已释放、同一 Issue 没有别的活跃任务；任一前提变化，接口返回 `resume_rejected`。通过后，服务重置本轮同步进度、开始新 Attempt；原来有交付分支时，为新一轮预留带新 Attempt ID 的分支，然后排入 `INITIAL`。服务返回更新后的 Flow，接口再读出详情。此时可核对的是**新一轮已排队**，要确认工作已发生仍需等 worker 交付。

### 两个浏览器标签页同时开着时

假设同事在 A 页看见第一个 Attempt 已归档，你在 B 页重新开工。A 页的归档和重新开工请求都携带当时看到的 `expected_attempt_id`。即使你在 B 页开始的第二轮后来也已归档，A 页的旧请求仍不能意外开启第三轮。遇到 `resume_rejected` 时，先刷新详情，比较这两个 ID；发现已经换轮，就查看当前轮的任务，不要替换请求中的 ID 后自动再发。如果 ID 没变，再查旧轮是否归档且已释放、同一 Issue 下是否已有活跃 Flow。这几种原因对用户的响应相同，单凭状态码不能推断 worker 已离线。[过期页面测试](../../tests/test_console_api.py#L797-L817)

移交给下一班时，把上轮结论和本轮待办分开写。例如「上轮 PR 已合并，归档成功；本轮目标是增加帮助文本，目前刚排队，尚未生成新的交付」。如果旧轮没有 PR，也可以给出具体验收依据，例如「复核时区配置后确认无需改代码，已记录结论」。新一轮详情里的当前归档原因、摘要和时间会清空；需要查清当时为什么结束，请回看旧 Attempt 的归档活动。值班人据此能分别确认上一轮已经完成什么，以及下一轮还需要等待谁行动。
