# 沿着一条任务读代码

代码里有几个名字都像在“处理任务”：HTTP 路由接到请求，工作流服务创建任务，调度器准备输入，最后工作端启动编码工具。区分它们最简单的方法，是看一项工作从这一侧交到下一侧时，究竟交出了什么。

## 包依赖边界

[领域层 `domain`](../../app/domain)定义 Flow、Attempt、Job 的含义，以及给定一组事实后可以做什么决定。它不用连数据库，也不需要知道 GitHub 的网址。[契约层 `contracts`](../../app/contracts)约定两端能交换哪些任务、结果和会话数据；[通信层 `communication`](../../app/communication)负责把这些消息送到正确的连接，把回复配回原来的请求。传输成功只能说明消息送达，不能替代业务层的交付确认。

真正组合这些能力的是[控制端 `controler`](../../app/controler)。它一侧依赖[持久层](../../app/persistence)提交本地事务，另一侧依赖[GitHub 客户端](../../app/github)访问安装了 App 的仓库。[工作端 `worker`](../../app/worker)隔着连接接任务，不直接改业务表。这个分工也由[包依赖测试](../../tests/test_boundaries.py)约束。

## 从收到通知，到允许执行

[事件入口](../../app/controler/events/webhooks.py)先保留原始通知，再由[路由器](../../app/controler/events/router.py)识别它的含义：Issue 标签可能触发创建，评论进入反馈缓冲，合并交给生命周期处理。同一个通知可以多次送达；重复投递不会因此变成多项新任务。浏览器则经过[控制台 API](../../app/controler/console/api.py)校验登录身份和写请求来源，直接提出创建、补充指令或重新开工等命令。

两条路都需要[工作流服务](../../app/controler/workflow/service.py)判断当前 Attempt 能不能接受操作。比如人在工作端正忙时补充一条要求，服务可以先保存输入，等能派发时一起交付。收到浏览器请求与编码进程读到新要求并非同一时刻。[任务协调器](../../app/controler/workflow/coordinator.py)为已领取的 Job 准备具体输入，包括仓库、交付分支和本次执行配置；只有分配仍有效，工作才可发出。

## 工作端怎样选目录、工具和模型

工作端上报的是具体[工作区库存](../../app/worker/execution/workspaces.py)。控制端在[领取事务](../../app/persistence/assignments.py)中保存 worker 身份、位置和该次绑定的代次。后续修订由原位置继续，另一项 Flow 则可以用同机其他位置。代次用于区分同一路径先后两位使用者：旧任务迟到的结果不能仅因目录名相同就被接纳。

[执行器](../../app/worker/execution/executor.py)通过同一个[编码接口](../../app/worker/coding_agent.py)启动或恢复 Codex、Amp。新会话使用本次任务指定的设置，恢复已有会话不会把后来的全局默认设置强加给它。项目也可配置 skill 仓库：工作端用单独的只读授权获取快照，再把常驻或选中的 skill 链接到工作区，见[安装逻辑](../../app/worker/execution/skills.py)。

只需要模型判断的工作有另一条路径。比如给任务命名，或判断评论是在提问还是要求修改，都不需要占用原 Flow 的代码目录。[推理调度](../../app/controler/workflow/inference.py)将它们作为推理 Job 派发给可用执行者：符合条件时优先使用控制端托管的 bub 推理 worker，也可以由支持推理的普通 worker 在独立会话中完成。如何由判断变成下一轮编码，见[评审循环](review-loop.md)。

## 从本地提交，到 GitHub 上的一张 PR

首轮编码可以通过[本地工具接口](../../app/worker/transport/mcp.py)申请开 PR，也可以请人补充输入。工作端核对 Git 或 jj 的实际提交和远端分支；[控制端工具服务](../../app/controler/workflow/tools.py)记录意图，[结果处理](../../app/controler/workflow/results.py)结合最终结果决定哪些操作可以生效。GitHub 评论、开 PR 等写入由[远端操作模块](../../app/controler/workflow/effects.py)完成。这样既不用把 App 私钥放进编码环境，也能在响应丢失时回头查清发生过什么。

<a id="47-storepostgresql"></a>

## PostgreSQL 里保存的是哪一种历史？

[数据库模块](../../app/persistence/database.py)管理共享连接池和事务；服务按职责使用具体仓储。在同一事务里，可以把阶段变化写入 `flows` 并追加一条解释变化的[Flow 事实](flow-ledger.md)。Job 的结果、工作区的占用和 GitHub 写入回执另有记录，不靠解释一段活动文本来决定是否重试。管理命令和测试可通过聚合的 [Store](../../app/persistence/store.py)访问这些仓储。

控制台看板和「会话」页都能读数据库里已发布的记录。后台刷新或 API 客户端要读取尚未采集的编码对话时，控制端才需要联系原 worker，因为原始会话保存在执行机器上。见[会话查询](conversation-api.md)。

独立运行的[对账模块](../../app/controler/reconciliation)定期核对已知 PR、反馈及可选的 GitHub Projects 看板，远端看板写入失败不会倒过来改变 Flow。类似地，[生命周期插件](../../app/plugins/hookspecs.py)可观察业务事件，插件执行成功不能充当 PR 已创建的证据。
