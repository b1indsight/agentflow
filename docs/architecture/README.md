# AgentFlow：谁记住任务，谁动手改代码？

假设你给 GitHub Issue 加上 `flow` 标签，然后关掉浏览器。稍后回来，可能看到一张等待审核的拉取请求（PR），也可能看到一个问题在等你决定。两种情况都不需要保持浏览器打开：AgentFlow 把任务记在控制端，把改代码的工作交给能访问仓库的工作端。两个进程可以同机运行，也可以分别部署。

第一次来，先读[控制端和工作端怎样分工](overview.md)，再看[架构图](diagram.html)。如果你更习惯边用边学，可以直接从[功能走查](../feature-walkthroughs/README.md)进入；那里从创建任务开始，一直写到审核、归档与重新开工。

## 顺着一个任务编号读源码

第一次读实现，不妨先问一个能验证的问题：控制台返回了一张新卡片，此刻系统究竟做完了什么？[控制台的 `create_flow`](../../app/controler/console/api.py#L225-L254) 先以指定的 GitHub 安装身份重新读取目标仓库，确认它没有归档；如果附带 Issue 编号，还要确认 Issue 可读。随后它把确认过的仓库名和任务交给 [`ControlService.create_flow`](../../app/controler/workflow/service.py#L106-L130)。后者把 Flow、首次开工和待执行工作一并写入数据库。HTTP 201 返回的是按 Flow ID 读出的 `FlowDetail`。从这张卡片可以断定任务已经登记，不能断定工作端已经启动，更不能断定 PR 已经创建。

往下读时，沿两个编号就能分清各篇文章的入口。用 **Flow ID** 找整项工作的详情和历次开工；用 **Job ID** 找其中某次执行。[`AssignmentStore.assign_job`](../../app/persistence/assignments.py#L89-L108) 给有资格的工作端分配一个 Job；[`ResultApplication.apply`](../../app/controler/workflow/results.py#L112-L133) 按同一个 Job ID 找回已经保存的结果。想知道连接中断时这两个方法之间还剩下哪些证据，读[架构决策](decisions.md)；想知道这轮的修改最后对应哪条评论，读[评审循环](review-loop.md)。

## 当前实现状态

| 想弄清楚的问题 | 从哪里读 |
|---|---|
| 一个请求到了服务端，哪个模块决定能不能执行？ | [沿着模块边界读代码](components.md)：从收件、事务到工作区和编码后端。 |
| 服务中途重启，怎样知道哪些动作能重试？ | [三个容易混淆的完成时刻](decisions.md)，以及[Flow 事实账本](flow-ledger.md)。 |
| 谁可以触发任务？为什么 worker 能推送却不用保管 App 私钥？ | [GitHub App 与仓库的边界](github-app.md)。 |
| 多条评审评论如何变成一次修订？ | [评审循环](review-loop.md)：反馈版本、批次、判定和原线程回复。 |
| 为什么能看到任务卡片，却可能读不到编码对话？ | [会话查询](conversation-api.md)：数据库和原执行机器分别保留什么。 |
| 子任务、跨机器迁移现在能用吗？ | [路线图](roadmap.md)区分现有行为和未来方向；[派生 Flow](derivable-flows.md)是设计提案。 |

## 从实现走到真实部署

阅读源码或运行行为测试，可以验证断线时任务是否重派、一个输入会不会被处理两次。真实 GitHub App 的安装权限、模型账号、集群网络和灾备则需要在目标环境中验证。准备运行系统时，请读[部署指南](../deployment/README.md)、[运维手册](../operations/runbook.md)和[真实环境验收清单](../verification/m5-live-e2e.md)。

<a id="m2-状态与待办"></a>
早期计划把评审修订称作 M2。今天的行为见[评审走查](../feature-walkthroughs/review.md)，当时的设计取舍与真实服务验收项保留在[功能记录](../plan/m2-review-resolution.md)。其他功能的提案和修订历史也保留在[计划目录](../plan/README.md)。

维护这些文档时，按[项目工作流](https://github.com/b1indsight/skills/blob/d56afec6c820b33a0ab44673aa280d0fb820603a/plan-first-workflow/references/feature-record.md)选择落点：模块职责、依赖或数据归属变化，更新架构文章；用户能观察到的行为变化，更新对应走查及其行为测试链接。一次变更的提案与取舍留在原功能记录，不把它当成已经上线的行为。
