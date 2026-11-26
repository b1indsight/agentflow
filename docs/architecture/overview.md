# 一项任务怎样在控制端与工作端之间流转？

把 AgentFlow 放在一次普通的代码评审里看：你提出改动，编码工具在仓库中提交一个版本，你和同事在 GitHub 上决定是否接受。AgentFlow 接手的是这些步骤之间的衔接。它记住该用哪份代码、上一轮问了什么、该回哪条评论；是否合并，仍由人和目标仓库的审核规则决定。

## 控制端记住约定，工作端保留现场

`agentflow serve` 运行**控制端**。它接收 GitHub 通知和控制台请求，并把业务状态写入 PostgreSQL。GitHub 收到 HTTP 202 时，通知已经存好，即使后台还没来得及处理，服务重启也有记录可查。此时不能推断编码已经开始。

`agentflow worker` 运行**工作端**。它主动连到控制端，报告这台机器上哪些仓库位置可以使用。控制端分给它一项工作和一个具体目录，它才在那份仓库里调用 Codex 或 Amp。一个 worker 可以提供多个位置；同一个位置在安全释放前只属于原任务。网络断开既不会搬走这个目录，也不能证明目录里的进程已经退出。

```text
GitHub 的通知 ──┐                          ┌── PostgreSQL：任务、分配记录、结果
                ├── 控制端（serve）─────────┤
浏览器的请求 ──┘          │                └── GitHub API：核对分支、创建 PR、回复
                          │ 已认证的 WebSocket：任务、工具请求、执行结果
                          │ 由工作端主动连接
                    工作端（worker）── 本地仓库、编码进程、原始会话
```

两个入口共享一套业务规则。Issue 标签可以触发任务；没有 Issue 时，用户也可以从控制台直接提交正文和图片。[控制端运行时](../../app/controler/runtime.py)把数据库、工作流服务和接收工作端消息的网关接在一起，服务重启后仍能从库里读回未完成的作业。[工作端运行时](../../app/worker/runtime.py)管理本机的执行与连接，把控制端发来的作业交给执行器。两端通过消息交换执行进度，不要求能直接读取对方的文件系统。

## 为什么有 Flow、Attempt 和 Job 三个名字？

假设任务是给导出功能加一个日期选项。**Flow 是这件事本身**。你补充日期格式、评审者要求修正时区，仍然在同一个 Flow 里。**Attempt 是这件事的一次开工**：本次分支、编码会话和交付的 PR 都有归属。归档后如果要重做，可在同一 Flow 下另开一个 Attempt；原来那张 PR 不会变成新一轮的交付。

**Job 是一次能派给 worker 的工作**。首次编码、按意见修订、收尾和模型推理有不同的输入，也有各自的执行结果。一次 Job 结束，只能说明这一步有了结果。例如首轮可以完成并留下一个给人的问题；这时 Flow 还在等输入，没有 PR，也不应自动归档。三个对象的定义见[领域模型](../../app/domain/models.py)。

### 一张新卡片怎样拿到第一个执行位置

**登记任务和领取任务可以发生在不同时间。**控制台入口负责确认请求来自有权限的用户，并以 GitHub 安装身份重新读取仓库。业务服务接过确认后的仓库名，把 Flow、首次 Attempt 和首轮 Job 写入同一个数据库事务。这三条记录分别回答「用户要完成什么」「这是哪一次开工」和「下一步要运行什么」。如果写入中途失败，数据库会一起撤销；调度器不会捡到一项找不到所属任务的工作。提交成功后，服务才通知在线工作端有新任务可领。[创建流程](../../app/controler/console/api.py#L224-L254)以及[原子创建测试](../../tests/test_console_api.py#L167-L199)给出了这两个阶段的分界。

调度器需要确认谁能写**这份**工作区。例如一台机器可以同时报告两个可写位置。一次首轮任务会占用其中一个；在这个 Flow 上继续修订时，调度器要沿用已有的归属。换用空闲目录固然能找到另一份相同版本的代码，却未必能找到上一轮的编码会话、本地修改和待确认的执行结果。因此，连接的身份、一次分配的凭据和目录的使用代次需要分别检查；只有 worker 名字相同，还不足以证明同一进程可以接管工作。[分配模块](../../app/persistence/assignments.py)负责在数据库里选择可用工作，[工作区模块](../../app/worker/execution/workspaces.py)则在本机检查目录归属。两者检查的是不同机器上的事实。

### 一条连接如何成为可用的工作端

工作端拨号后，控制端的 WebSocket 入口先把连接交给 [`serve_worker`](../../app/controler/api.py#L253-L259) 验证身份。通过后，[`ServerRuntime.serve_worker_session(worker_id, register, session)`](../../app/controler/runtime.py#L146-L180) 才接手：`worker_id` 标识机器，`register` 是它报告的能力，`session` 是本次连接，三者不可互换。运行时把处理执行消息与文件消息的回调装到这条连接上，再调用 `Gateway.serve`。因此能连通网络，并不自动获得领取工作或查询文件的权限。

同一台机器重连时会遇到一个实际问题：旧连接可能尚未关闭。[`Gateway.register`](../../app/communication/gateway.py#L104-L142) 先保留新连接的位置，等待前一条连接关闭，再检查新连接是否仍是当前连接。只在条件满足时，调用方才拿到可用的登记对象。接下来 [`ServerCallbacks.on_published`](../../app/controler/workers/callbacks.py#L202-L208) 把工作端能力与本次连接代次登记到存储。**替换连接只改变消息往哪里送**；是否有权继续某个 Job，还要对照工作区、Job 和原分配凭据。

### 从消息里区分「开始」与「结果」

以首轮编码为例，控制端发出 `worker.execute` 时，会先登记这次调用预期收到的结果类型。[`WorkerStateCallbacks.on_execute`](../../app/worker/transport/state.py#L324-L360) 收到 `Assignment` 后核对调用身份，并把参数交给本机执行对象；对于首次接收的工作，它先发送启动通知，再启动执行。控制端的 [`on_started`](../../app/controler/workers/callbacks.py#L533-L545) 把 Job ID、Attempt ID、worker、分配凭据和连接代次一并交给 `start_assignment`。只有这些条件和当前工作区都匹配，才写入启动时间、延长租约；修订次数也在这笔事务内增加。迟到的旧通知不能给新任务“签到”。[启动事务](../../app/persistence/assignments.py#L257-L311)

进程结束后，消息回答的是另一个问题。工作端用 [`ExecutionState.complete`](../../app/worker/state/execution.py#L194-L219) 把本次执行的结果或异常变成一份可重送的记录；控制端的 [`on_result`](../../app/controler/workers/callbacks.py#L547-L573) 核对身份，持久接收记录，再交给工作流处理。因而发生重启时，已经收到的执行结果可以继续用于开 PR 或回复评论，不必再启动一次编码进程。[重连测试](../../tests/test_controller_update_boundary.py#L38)特意把连接切在“已启动、未交回结果”之间。

### 可运行的例子：同样是超时，为什么一个任务重新排队，另一个保持阻塞？

工作端需要定期续约。**租约过期只说明控制端没有按时收到续约，不能说明执行器是否已经动过代码。**控制端的恢复模块从持久记录中提取事实，再由一个不访问网络或数据库的函数决定 Job 的处置。下面把时间固定，直接调用项目里的判定函数。在仓库根目录用 `uv run python` 执行即可，不需要启动控制端、连接 GitHub 或准备数据库。

```python
from datetime import datetime, timedelta, timezone

from app.domain.models import FlowState, JobKind
from app.domain.recovery_policy import JobRecoveryFacts, decide_job_recovery

now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
before = now - timedelta(minutes=1)
job = JobRecoveryFacts(
    id=81, flow_id=42, kind=JobKind.INITIAL,
    flow_state=FlowState.RUNNING, delivery_state=None,
    lease_expires_at=before, recovery_deadline=now,
)
for changes in ({}, {"dispatch_authorized_at": before}, {"result_received_at": before}):
    observation = job.model_copy(update=changes)
    print(decide_job_recovery(observation, now).value)
```

预期三行输出依次是 `requeue`、`blocked`、`keep`。第一份记录没有派发授权、启动记录或待确认的停止请求；租约已过期，系统可以安全地把 Job 放回队列。第二份只多了一项证据：控制端曾授权发送任务。即使控制端没收到启动通知，工作端也可能已经开始执行；恢复期限到了仍无法核对，重新启动编码有重复写入的风险。第三份已经有结果到达，应保留这份 Job 记录，让负责应用结果的模块继续处理交付，不靠第二次编码补写结果。[恢复判定](../../app/domain/recovery_policy.py#L61-L88)还单独检查工作区是否能释放；示例的三个返回值都不是「删除目录」的指令。

在实际部署里，控制端的恢复循环先读取事实，再把判定应用到数据库。工作端能否重新连上、能否读回本机执行记录，则取决于原机器和磁盘是否仍然可用。上面的例子可以重现规则，却不能用来证明某台离线机器已经停止。要观察两端真正的消息顺序，可以运行[控制端替换测试](../../tests/test_controller_update_boundary.py#L38-L96)：测试暂停正在执行的作业，断开原连接，换上新控制端，让**原工作端**继续，最后同时检查编码调用只有一次、Job 已完成、PR 也只创建了一次。

## 读懂看板上的一个状态

`queued` 表示排队，`running` 表示正在处理，`review` 表示等待评审或人的输入。一项工作若无法安全继续，会进入 `blocked`。`finalizing` 是正在收尾，`archived` 才是归档；归档原因另记为成功或放弃。看到 `review` 时，要看本次交付有没有 PR；看到成功归档时，还要看原工作区是否已经释放。**业务阶段、远端交付和本机进程是三件可以分别变化的事。**

接下来可以[跟着一次首轮交付走](../feature-walkthroughs/first-delivery.md)，也可以转到[模块地图](components.md)，看看是谁维护上述三种记录。
