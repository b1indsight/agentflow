# 首次交付：从一条导出需求找到 PR 或待答问题

首次交付模块把一条需求送到指定仓库的工作区，再把执行结果交回给提需求的人。控制端负责认领任务、核对远端分支和登记交付；worker 调用编码工具修改文件、提交并推送。交接时，你可能拿到一张可以审代码的 PR，也可能拿到一个必须先回答的问题。这两种结果都能让首个作业正常结束。要判断轮到谁行动，请看 Flow 详情里的 PR、当前动作和对话，不要只看作业是否完成。

## 亲自创建一次：为 CSV 加一个可验收的日期列

选择已安装 GitHub App 的测试仓库，确认仓库里确有 CSV 导出功能，也有能处理它的 worker。以下操作会真实创建任务，并可能推送一个新分支和开 PR。先确认你愿意在这个仓库做实验；请勿把客户的真实导出记录放进正文。用部署指定的所有者身份登录控制台，选择仓库，以直接输入需求的方式提交下列正文。也可以新建一个 Issue，粘贴同一段正文，由在允许名单内的人加上触发标签（默认 `flow`）。两种入口任选一种；直接任务不会因为正文相同就与 Issue 合并。

> 为 CSV 导出增加可选的 `export_date` 列。不开启选项时，列名、列顺序和每一行的内容都要保持原样。开启时在末尾增加该列，用记录时间换算出 UTC 日期，再按 `YYYY-MM-DD` 输出。请用 `2026-03-01T00:30:00+02:00` 做一条跨日测试：该行应显示 `2026-02-28`。请同时测试默认关闭的导出，并在 PR 正文注明怎么运行这两个用例。

这条正文给实现者留了选择代码位置的空间，却固定了评审者能独立检查的结果。输入时间换算后是二月二十八日二十二点三十分；直接截取输入字符串的前十个字符会得到三月一日，测试就能抓到这个错误。关闭选项时也要比对一份旧输出，否则实现者即使把新列无条件加给所有调用方，也可能通过只测开启选项的用例。日后审 PR 时，拿同一条输入和同一份旧输出核对即可。

如果你正在联调控制台，可以**用下面的示例代替页面提交**。在已登录、同源的控制台页面打开浏览器开发者工具，替换仓库和安装 ID（可在页面选择目标时，从 `/api/flow-targets` 的响应 `items` 中读取这一对值）。整个代码块只执行一次；不要在已经点过提交后为了取得 ID 再运行。代码保留服务端的错误正文，方便区分输入校验和仓库访问问题。

```javascript
const target = { repo: "your-org/csv-sandbox", installation_id: 42 };
const request = {
  ...target,
  task_content: "在 CSV 末尾添加可选 export_date 列，默认输出不变。" +
    "按 UTC 输出 YYYY-MM-DD；用 2026-03-01T00:30:00+02:00 验证得到 2026-02-28。" +
    "请测试开启和关闭两种情况，并在 PR 里写明测试命令。",
};
const created = await fetch("/api/flows", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(request),
});
if (created.status !== 201) throw new Error(`${created.status}: ${await created.text()}`);
const { id } = await created.json();
async function inspectDelivery() {
  const r = await fetch(`/api/flows/${id}`);
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  const { current_attempt_id, phase, action, pr_url, next_condition } = await r.json();
  console.table({ id, current_attempt_id, phase, action, pr_url, next_condition });
}
await inspectDelivery();
```

正常时你会收到 `201` 和新的 Flow ID。创建成功说明控制端接受了正文；重新运行最后一行只会读取详情，可在稍后确认 worker 是否已经开工。刚创建的直接任务没有 Issue 链接；在 worker 领取前看见 `queued`、没有 PR，是预期结果。如果仓库已经归档，重新选一个可写的测试目标。如果收到输入校验错误，检查自己有没有同时传 `issue_number` 和 `task_content`：服务只接受其一，附件也要求直接正文。正文上限为 32 KiB，按 UTF-8 字节算，中文字符不能按一个字节估算。[请求约束](../../app/contracts/console.py#L85-L105)和[直接创建测试](../../tests/test_console_api.py#L167-L197)分别说明了这两道边界。

## 从任务详情辨认是否开工

打开这一次创建的详情，抄下 Flow ID 和当前 Attempt ID。交班时前者让同事找到需求，后者让同事确认你们在看同一轮执行。走 Issue 入口后如果连活跃任务都找不到，先核对标签名和**加标签的人**；Issue 作者有权限并不代表代为加标签的人也有权限。走直接入口时如果浏览器在提交途中断线，先在任务列表按仓库和时间核对；服务没有为直接创建提供按正文重放的保证。

[Issue 的处理方法 `_on_issues`](../../app/controler/events/router.py#L60-L76) 从标签事件取仓库名、Issue 号和安装 ID，核对标签和操作者；不在允许名单内，方法就返回，不创建 Flow。控制台走[另一个入口 `create_flow`](../../app/controler/console/api.py#L225-L253)：先异步向 GitHub 核对仓库、按需读取 Issue，再把正文和附件一起交给 `ControlService.create_flow`。两条路在[同一段事务](../../app/controler/workflow/service.py#L106-L130)汇合：写入 Flow、保存附件、排入首个 `INITIAL` 作业，成功才通知调度。若同一 Issue 已有活跃 Flow，服务返回 `None`，控制台报 `flow_conflict`；没有 Issue 号的两次独立创建不按正文去重。

有 worker 可用时，[连接上的 `poll`](../../app/controler/workers/callbacks.py#L325-L335)用 worker ID、分配令牌和当前连接向 `assign_job` 领取作业，取不到就结束本次查找，取到才异步准备。[`InitialPreparation.prepare(job, flow)`](../../app/controler/workflow/initial.py#L71-L118)先确认作业属于本轮、工作区仍归这台 worker，再读任务正文和远端仓库；首次开工还要等分支命名结果。[返回 `InitialTask` 前](../../app/controler/workflow/initial.py#L119-L156)，它查询远端同名分支，并在锁内再次确认 Attempt、记下目标分支、合入已登记的要求。

准备好后，[`_on_prepared`](../../app/controler/workers/callbacks.py#L818-L840)检查是否仍允许派发，才发出带任务及工作区身份的 `worker.execute`。工作端的 [`on_execute`](../../app/worker/transport/state.py#L324-L345)读取本地执行记录；若已开始过，重收同一指令不会再启一个 Agent。

## 路径一：有明确改动，需要你审阅

编码工具在分配到的目录里工作，提交并推送到本次指定的分支，然后请求开 PR。等详情出现 PR 链接，再点开 GitHub 检查：目标仓库和分支应当正确，PR 顶端应是这次交付的提交。此时任务进入 `review`，由你审核。工具调用的回执或会话里一句“已经完成”，都不能代替这张实际的 PR。[远程交付测试](../../tests/test_remote_e2e.py#L167)检查从领取到关联 PR 的整条路径。

为什么工具成功了还要等？第一次调用 [`_open_pr`](../../app/controler/workflow/tools.py#L226-L266) 时，控制端复核本次分配仍有效，把已核对的提交 SHA、标题、正文存下，返回 `PreparedDelivery`，里面还没有 PR 号。等 worker 交回终态结果，[`initial_plan`](../../app/controler/workflow/initial.py#L254-L293) 才把结果和保存的工具调用对上，生成创建请求。控制端[先存计划，离开事务再执行远端操作](../../app/controler/workflow/results.py#L225-L233)：按分支查找已有 PR，确实尚未发出请求才调用 [`repository.open_pull_request`](../../app/controler/workflow/effects.py#L392-L405)。上次已发出却无法确认结果时会等待核对，不盲目再开一张。[测试最后逐项核对](../../tests/test_remote_e2e.py#L225-L238)了 PR 号、远端 SHA、工具回执以及实际开 PR 的次数。

## 路径二：还缺一个人的决定

例如需求只写了“当地时间”，却没说是读者所在时区还是仓库配置的时区。编码工具可以留下这个问题，暂停而不创建 PR。详情会显示待办消息；此时同样可能是 `review`，成功的标志是**人看得到问题，而且没有凭空多出一张 PR**。在同一个 Flow 里补充答案，原会话可接着处理，已经留下的本地工作也不应被丢弃；若决定不做，可结束本轮。[等待输入的行为测试](../../tests/test_remote_e2e.py#L248)覆盖继续交付和直接归档两种结果。

这条路在同一个 [`initial_plan` 的 `input_request` 分支](../../app/controler/workflow/initial.py#L260-L281)分开：返回的问题和已推送的提交一起记入等待状态，计划里没有开 PR 的动作。所有者补答后，[`_queue_agent_inputs`](../../app/controler/workflow/service.py#L362-L377) 为这次等待中的交付排一个带 `owner_messages` 的 `INITIAL`；[worker 用原 session 调用 `resume`](../../app/worker/execution/executor.py#L183-L198)。所以测试里有两个首次交付作业，却只启动过一次新会话，最后也只有一张 PR；若选择归档，则始终没有 PR。[两条分支的断言](../../tests/test_remote_e2e.py#L296-L322)

还有一种情况确实是失败：首次准备时，远端已经有同名分支。系统不会把它当作自己的工作，原分支保持原样，也不创建 PR。详情里的准备错误应与暂停提问区别处理；不要为了让任务通过就删除或强推那条分支。见[同名分支保护测试](../../tests/test_remote_e2e.py#L326)。

## 当班交接：下一步交给谁？

交给评审者时，附上 Flow ID、Attempt ID、PR 链接和跨日测试的输入、预期输出。让对方先核对 PR 的源分支、目标分支和 diff，再看默认导出的回归用例。打开了 PR 代表可以开始评审，不能据此记录“测试通过”或“已经合并”。若验收发现时区需求写错了，在原 PR 提意见，后续修订就有明确的参照版本。

交给需求方时，贴出工具留下的原问题，并说明当前没有 PR。例如选择“按记录中的偏移取日期”会使示例输出改为三月一日，不能只答“是”。请在该 Flow 的追加任务入口同时给出时区规则和跨日预期，让续跑能据此写测试；不要为同一个问题再开一个 Issue。交给维护者时，则提供失败的准备步骤、目标仓库及冲突分支，让维护者先核实分支归属。三种交接都用同一组标识，接班人不需要根据聊天时间猜测对应的是哪一轮。
