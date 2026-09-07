# Review 修订循环

M2 Feature 1 已实现持久反馈批次与 settle，见[实施计划](../plan/m2-feedback-batches.md)。
真实 bub 语义接口、修订交付核验/session 兜底与线程 resolve 分别属于 M2 Features 2–4，尚未验收。

## B.1 反馈入口与持久 settle

普通非 bot 反馈经过如下路径：

```text
反馈 delivery + 不可变版本 + buffer deadline（同一事务）
  → 持久 settle 到期
  → 冻结 batch + 创建 TRIAGE job（同一事务）
  → Judge → revise / answer / wait / ignore
```

review body、inline 和 conversation 使用 `(flow_id, kind, github_id)` 区分身份，保留来源 attempt。
重复 delivery/内容和较旧源时间不重置窗口；同秒编辑可使用 GitHub `changes.body.from` 校验前序正文。
没有更新时间的编辑以接收时间排序。删除保存 tombstone，后到的旧事件不能复活正文。

allowlist 作者本人提交的 Request Changes，或正文非引用/代码块中的行首 `@<bot_login> 指令`，
使用较短 submit grace 合批后直达 REVISION。非 allowlist 用户的同类文本只作为普通讨论；
edited 不产生新的强信号，approve/dismissed 不触发修订。空 Request Changes 保存信号但不创建空修改，
之后收到可操作正文/inline 再处理。配置 bot 和 GitHub `type=Bot` 的事件均不进入反馈循环。

到期时间在 PostgreSQL；启动时以及 worker sweep 周期扫描，不依赖进程内 timer。
到期后最多增加一次 sweep 周期的调度延迟。一个 Flow 最多一个 ready/processing 批次，
批次从 TRIAGE 到派生 REVISION/ANSWER 一直保有这一约束，直到结果与必要副作用完成。

## B.2 冻结批次与任务协议

协议 v5 的 TRIAGE/REVISION/ANSWER task 携带 batch ID、attempt ID、完整反馈快照，result
携带同一 batch ID 与精确 item/version 清单。control 校验所有权、当前 attempt 和快照成员。
prepare 与 result apply 不再重新读取“所有未 relay 评论”作为本轮输入或完成集合。

例如：A/B 已进入批次，执行期间到达 C 或 A 的新版本；旧结果只确认 A/B 的旧版本，
C 和新版 A 留到后续批次。模型已看到（relayed）、当前版本已消费（consumed）、远端 thread
resolved 是不同事实；M2 F1 不把 addressed 写为 GitHub resolved。

Flow 生命周期保持 `ACTIVE/SUCCEEDED/CANCELLED/FAILED`；PR delivery 独立保存
`NONE/OPENING/OPEN/CLOSED/MERGED`。settling/waiting/running 从 buffer、batch 和 job 推导，
不恢复旧的 `PR_OPEN/REVISING` Flow 枚举。

## B.3 判定与反馈消费

| 结果 | 当前行为 |
|---|---|
| revise 且达到置信度阈值 | 通过持久效果账本发送修改说明，原子派生一个携带同批次的 REVISION |
| answer | 派生携带同快照的 ANSWER，沿用 Judge.answer；答案发布成功后精确消费本批次 |
| 空答案 | 保留为 waiting，不发空评论或消费反馈 |
| wait / 低置信度 revise | 保存 open_points/reason，保持 waiting，无新 generation 不自动重跑 |
| ignore | 消费本批次精确版本，不发外部消息 |

新反馈到来后接续 waiting 批次的未消费版本和 open_points。旧批次结束后由新 generation
建立下一批；没有新信息时不重复调用模型。bub 解析失败仍保守 wait；其 CLI/模型契约、
timeout、严格 confidence 校验、丰富 PR 上下文和去除答案二次生成属于 F2。

## B.4 修订结果

修订使用原 session 调用 CodeAgent.resume，prompt 含冻结的 instruction/feedback 和 VCS 指引。
`CodexResult` 的 addressed/deferred 必须携带 `item_id`、`version`、GitHub `comment_id` 和
note/reason；control 拒绝未知、重复、跨版本或互相冲突的引用。

非零 exit 按现有失败路径收尾，不消费反馈。成功时只消费明确 addressed 的版本；deferred、遗漏项
及 needs_clarification 留作未决，有未决问题时不请求 re-review。处理说明仍主要发在 PR conversation，
真实原线程回复和 resolve 属于 F4。

本轮 push capability、resume 环境、VCS/远端 SHA 校验、失效 session fallback 尚属 F3。
当前成功 exit 不能证明真实 revision 已完成 commit/push，整个 M2 不能因此标为已验收。

## B.5 取消、重试与恢复

- Flow 终态时退休 buffer/活动批次；取消仍不 kill 运行中的 agent，旧结果受现有 assignment/状态 fencing。
- 显式 retry 将原冻结批次交给新的 job，保留后到的新版本，不复制新的 Flow/PR。
- 已接收的结果依然通过原子结果应用与 GitHub effect ledger 恢复，不能因重放重复派生或消费。
- schema 一次性导入旧 review_threads 的最新对象，保留历史表；旧 resolved 标记不作为远端确认。
- 协议 v5 要求 serve/worker 一起升级。先用旧版本排空 queued/running review jobs 和所有未确认旧结果，
  然后停止旧进程再升级；新 serve 检查旧任务并给出 job ID，禁止猜测缺失的 batch/版本。

## B.6 参数与验证

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `FLOWTOOL_SETTLE_SECONDS` | 75 | 普通静默窗口 |
| `FLOWTOOL_SUBMIT_GRACE_SECONDS` | 15 | review submitted/强信号窗口 |
| `FLOWTOOL_WORKER_SWEEP_INTERVAL` | 5 | 持久 deadline 与结果恢复扫描间隔 |
| `FLOWTOOL_CONFIDENCE_THRESHOLD` | 0.6 | revise 最低置信度 |
| `FLOWTOOL_CODEX_RUN_TIMEOUT_SECONDS` | 1200 | 单次 Codex timeout |
| `FLOWTOOL_MAX_REVISION_CYCLES` | 20 | 每 attempt 的修订次数限制 |

PostgreSQL 自动化覆盖入口回滚、并发领取、重启、批次内外版本、删除、wait 接续、强信号授权、
结果重放和 retry。真实 bub/Codex/GitHub 多轮闭环及线程操作留到 M2 后续 feature 验收。
