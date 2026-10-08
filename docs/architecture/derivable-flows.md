# 可派生 Flow 编排

> 状态：未来目标设计，不代表当前实现 · 设计日期：2026-09-03。
>
> 当前系统只实现由 GitHub Issue 触发、由 job 驱动的单 Flow 路径；本页定义后续演进时必须保留的
> 语义边界。阶段性落地顺序以[路线图](roadmap.md)和具体计划为准。

## 1. 目标与不变量

FlowTool 将 **Flow** 提升为持久、可调度、可派生、可通信、可等待和可验收的第一公民。它借鉴 Codex
subagent 的 `spawn / send / wait / inspect` 协作方式，但编排状态保存在 control plane。首版先采用
Flow 全生命周期固定绑定，跨 worker/机器恢复属于后续阶段。

以下不变量长期成立：

- 一个 Flow 对应一个逻辑隔离执行环境、一个独占 repo 目录、一个稳定 branch，以及最多一个 PR；需要
  另一个 branch、repo 写入边界或 PR 时必须派生另一个 Flow；首版“逻辑隔离”表示调度和目录所有权，
  不表示抵御同 VM 上恶意代码的安全边界；
- Flow 是任务、上下文、依赖、结果和交付物的持久所有者；
- Agent run 与 worker 只是 Flow 的临时执行载体，不是可持久寻址的协作者身份；
- Flow 可以要求 control plane 创建子 Flow，但只有 control plane 能持久化关系和安排执行；
- 父子 Flow 不共享可写 repo；首版分别固定在各自 worker execution slot，未来可以跨进程、跨 worker、
  跨机器恢复；
- 父 Flow 注入的上下文与 skills 是显式、版本化、可审计的执行输入；
- 子 Flow 执行成功与父 Flow 验收成功是两个不同事实；
- PR、评论、labels、reviewers 等 GitHub 副作用始终由 control plane 校验并执行，Agent 只调用受约束的
  环境工具。

## 2. 领域对象

| 对象 | 职责 |
|---|---|
| `Flow` | 持久任务及其生命周期；拥有零或一个交付 PR |
| `FlowExecution` | Flow 独占执行环境的绑定；首版持有 worker slot 和 repo identity 直到 Flow 终态 |
| `FlowOrigin` | Flow 的创建来源；可以是 GitHub Issue、父 Flow 派生或未来其他入口 |
| `Attempt` | 一个 Flow 的一次执行/恢复尝试；保存 session 和运行事实，引用 FlowExecution，不拥有 lease 或候选 PR |
| `Job` | 可领取的短期执行单元；由 lease、fencing 和 result inbox 保证安全重放 |
| `FlowLink` | Flow 之间的派生、依赖和验收关系；保存 join 与 acceptance 状态 |
| `ContextBundle` | 可移植的初始上下文和后续增量，包含来源、可见范围与内容摘要 |
| `SkillManifest` | 子 Flow 需要启用的 skill 名称、版本、digest 与策略决定 |
| `FlowResult` | 版本化结果 envelope：总结、结构化输出、证据和 artifact 引用 |
| `FlowWait` | 父 Flow 的持久 join 条件；满足后由 control plane 创建 continuation job |

派生关系形成有根树；额外的依赖和上下文引用可以形成 DAG，但 control plane 必须拒绝循环依赖。Flow ID
是跨执行节点的稳定 handle，Agent/session/worker ID 不能替代它。

GitHub Issue 只是 root Flow 的一种 origin，不是 Flow 身份本身。子 Flow 不需要伪造 Issue number；分支、
tool authorization 和 delivery ownership 都以稳定 Flow ID 为键。

## 3. 状态分轴

当前 `FlowState` 把执行和 GitHub 交付阶段压在同一个枚举中。可派生 Flow 需要把同时成立的事实分开：

| 维度 | 示例状态 | 权威位置 |
|---|---|---|
| Flow 生命周期 | `active / succeeded / failed / cancelled` | `flows` |
| 执行状态 | `queued / running / done / failed` | `jobs`、attempt lease |
| PR 交付状态 | `none / opening / open / merged / closed` | flow-owned delivery |
| 等待状态 | 等待哪些 Flow、`all / any` join | `flow_waits` |
| 验收状态 | `pending / accepted / rejected` | `flow_links`，按父子关系记录 |

例如一个父 Flow 可以同时满足“PR 已打开”“当前没有运行 job”“等待两个子 Flow”“其中一个结果尚未验收”。
这些事实不能继续编码成互斥的 `PR_OPEN_WAITING_CHILDREN` 一类组合状态；面向用户的单一 status 应由上述
事实派生。

## 4. Agent 环境工具

CodingAgent 看到的是 Flow 语义工具，不是 control 数据库、worker 管理接口或 GitHub credential：

```text
flow.spawn(task, context, skills, result_contract, relation) -> child_flow
flow.send(target_flow, context_delta)                       -> accepted
flow.wait(flow_handles, condition)                          -> suspended
flow.inspect(target_flow)                                   -> snapshot/result
flow.accept(target_flow, result_version, note)               -> accepted
flow.reject(target_flow, result_version, feedback)           -> revision queued
flow.cancel(target_flow, reason)                              -> requested
```

GitHub 协作走同一能力通道，例如 `flow.open_pr`、`flow.comment` 和 `flow.request_review`；control plane
根据调用者绑定的 Flow 推导 repo、branch 和允许操作的 PR，Agent 不能用参数越权到任意仓库或 PR。

工具调用路径为：

```text
CodingAgent
  -> worker-local Flow tool broker（MCP）
  -> 已认证、可复用的 worker/control 通道
  -> control command handler + Store
  -> durable effect / scheduler / GitHub adapter
  -> tool response
```

broker 只转发调用，不持有业务状态。每次请求由 worker 附加 `flow_id / job_id / assignment_token /
connection_epoch / call_id`，control plane 做 fencing、权限检查和幂等处理；这些控制字段不由模型自由填写。

## 5. Spawn、等待与恢复

`flow.spawn` 成功返回前，control plane 必须在一个事务中保存子 Flow、`FlowLink`、初始 context、skill
manifest 和首个 job。返回成功意味着派生事实已经持久化，不意味着已有 worker 开始执行。

`flow.wait` 不允许长期占住 Codex 进程或 WebSocket handler：

1. 父 Agent声明 child handles 和 `all / any` 条件；
2. control plane 保存 `FlowWait`，当前 job 以 `waiting` outcome 安全结束；
3. 子 Flow 产生新结果或状态变化时，control plane 重新求值 join；
4. 条件满足后只创建一个父 Flow continuation job；
5. continuation 注入相关结果版本和等待期间的 context delta。

首版中，父 job 结束不等于释放执行环境：父 Flow 仍独占原 worker slot 和 repo，continuation 只回到该
worker。父 Flow 派生子 Flow 时，子 Flow 必须取得另一个 slot 和自己的 repo；没有空闲 slot 时排队。
worker 断线只允许同一 identity 在 recovery grace 内接回原目录，超时后 Flow 明确失败，不迁移。

后续引入 checkpoint/artifact 后，才允许释放 slot 并在其他 worker 上重建干净 session。该阶段的可迁移
语义不能只依赖某台机器上的 `codex_session_id` 或裸本地路径。

## 6. 上下文、skills 与 artifacts

跨机器 Flow 不能传递裸本地路径。ContextBundle 只携带内联小文本或可寻址引用，例如 commit、PR、Store
snapshot 和 artifact URI；引用必须有 digest、来源 Flow 和访问范围。

父 Flow 可以在 spawn 时提供最小上下文，也可以在子 Flow 运行期间追加 delta。control plane 按事件顺序
保存增量；子 Flow 的下一次 start/resume 收到尚未消费的版本，重复投递必须幂等。

SkillManifest 表达任务需要的执行能力，不表达 Agent 人格：

```text
name + version + digest + source + required/optional
```

control plane 先按仓库和系统策略校验，再选择已经具备这些 skills 的 worker，或下发经过验证的不可变 bundle。
worker 必须回报实际启用的 manifest；缺少 required skill 时 job 保持不可调度或明确失败，不能静默降级。

大结果、diff、测试日志和构建产物进入 artifact store；`FlowResult` 只保存结构化摘要和引用，避免把原始日志
灌入父 Agent 上下文。

## 7. 结果与父级验收

子 Flow 完成执行后发布不可变、递增版本的 `FlowResult`，进入 `result_ready`。父 Flow 被唤醒后可以 inspect
结果、PR diff 和验证证据，然后：

- `accept`：把对应 `FlowLink` 的 result version 标为 accepted，满足依赖；
- `reject`：保存反馈并给同一子 Flow 排 revision job，不创建第三个 Flow 或第二张 PR；
- 暂不决定：继续保持 pending，父 Flow 可以再次 wait。

验收属于父子边，因此子 Flow 可以保持“自身执行成功，但仍待父 Flow 验收”。验收不等于 GitHub approve、
merge 或关闭 PR，这些仍遵循独立的仓库策略和人工审核流程。

## 8. 调度、权限与故障语义

- scheduler 调度 Flow 的 job，不调度“人格化 Agent”；worker 依据 repo、skill manifest、容量和隔离要求匹配；
- 首版 worker 是单槽：一个 Flow 首次启动即独占该 slot 和完整 repo 到 `succeeded / failed / cancelled /
  archived`；没有 running job 或正在等待子 Flow 时也不释放；
- 同一 Flow 的 initial/continuation/revision job 串行进入同一 repo；不同 Flow 不能共享 working tree 或
  可写 `.git/.jj` metadata；
- 同一可信 VM 可以运行多个使用不同目录的 worker；强文件系统/网络隔离和互不信任租户属于后续容器/
  微 VM 阶段；
- 父 Flow 只能操作 control plane 授予的相关 Flow handle，不能枚举或控制无关 Flow；
- spawn/send/accept/reject 都需要稳定 idempotency key，断线重试不得产生重复子 Flow或重复 revision；
- 配置最大派生深度、单 Flow fan-out、root Flow 总并发、预算和 artifact 配额；
- 父取消默认向尚未验收的子树传播，但已产生外部 PR 的 Flow 进入显式 cancel/cleanup 流程；
- 子 Flow失败不会隐式覆盖父 Flow；`FlowLink` 的策略决定 fail-fast、等待人工处理或允许父级继续；
- control plane 重启后必须能从 Store 重建 wait/join、未消费 context、tool call 和 acceptance 状态。

## 9. 分阶段落地

1. **单 Flow 能力地基（M1）**
   - 固定“一 Flow = 一隔离执行环境 = 一独占 repo = 一 branch = 最多一 PR”，把 Attempt 收回为执行尝试；
   - Flow 全生命周期独占单槽 worker；断线超时明确失败，不实现 checkpoint 或迁移；
   - 分离 Flow 生命周期、job 执行和 PR delivery 状态；
   - 建立通用 Flow tool RPC、fencing、幂等记录与 MCP broker；
   - 以 `flow.open_pr` 作为第一个 tool，尚不实现 Flow 派生。
2. **持久 Flow 派生**
   - 实现 `FlowLink`、`flow.spawn/inspect/send/cancel` 和子 Flow 调度；
   - 子 Flow 在另一个匹配的空闲 slot 上，以独立 repo/session 启动。
3. **等待与验收**
   - 实现 `FlowWait`、durable join、父 Flow continuation 和版本化 `FlowResult`；
   - 实现 `accept/reject` 及 revision feedback。
4. **完整可移植执行**
   - ContextBundle、SkillManifest 和 artifact 分发生产化；
   - 增加可释放的 checkpoint、跨 worker 重建、旧 lease fencing、配额、审计和故障恢复。

## 10. 非目标

- 不把 Flow 当作常驻 Agent 进程；没有 job 时不占用 Codex 进程，但首版仍保留 worker execution slot 和 repo；
- 不要求为 Agent 建人格、角色或长期身份；
- 不让父 Agent选择具体 worker 或直接建立 Agent-to-Agent 网络连接；
- 不让子 Flow共享父 Flow 的可写 repo；共享必须通过 commit、artifact 或显式 context；
- 不因父 Flow验收而自动 merge GitHub PR。
