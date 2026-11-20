# 端到端流程

## 当前自动化覆盖的切片

```text
签名正确的 Webhook
  → FastAPI 持久化 webhook inbox 并返回 202
  → 后台消费者 → Event Router 去重 / 授权 / 写 PostgreSQL
  → flow + queued job
  → worker 按仓库需求补充并登记 workspace，serve 领取具体 (worker_id, workspace)
  → FlowExecution 独占该位置与 generation，prepare 读取真实 Issue/default branch
  → Started / Heartbeat；INITIAL 获得临发 scoped push capability
  → Codex commit/push → worker-local flow.open_pr MCP → control tool ledger
  → worker 核验本地 VCS/remote；Result 与持久 tool outcome 核对
  → control 再核对 GitHub ref → find-or-open 唯一 PR → AcceptResultResponse；进入 review，保留原工作区
  → review/comment → 持久反馈与 settle → 冻结下一批
  → control 本地 Judge 判定 revise/answer/wait/ignore；授权 Request Changes 经 grace 直达 revise
  → revise：同 repo/branch/PR resume 或一次性 session 兜底 → 双端交付核验
  → 原线程说明 → 覆盖与版本检查 → 保守 resolve 并复读 → 无未决反馈时请求 re-review
  → answer/ignore 精确消费本批版本；wait 保留问题，等待新反馈
  → 断线：未授权派发的任务可等 lease 后重排；已授权但缺 Started 的未知执行保留绑定
  → started 只由原 worker 在 grace 内续跑/重放结果；已接收终态结果由 control 恢复
  → serve 重启：恢复持久 assignment/result inbox，不重复派发 started job
```

测试覆盖内部 Issue→PR、M2 批次和修订控制流；GitHub/Codex 使用替身，bub 契约连接假 provider。
真实服务闭环尚未验收；同秒重复 webhook 重放已在 M4 F1 修复，见 [M2 状态与待办](README.md#m2-状态与待办)。

## 5. 目标闭环

1. 用户创建 issue 并添加 `flow` 标签；
2. GitHub 发送 `issues.labeled` Webhook；
3. control 派发 INITIAL task，worker 建立隔离工作区并启动 Codex 首轮编码；
4. Code Agent commit/push 后调用 `flow.open_pr`；worker 与 control 依次核验远端 SHA，先持久化
   PR 意图；INITIAL 终态结果核验成功后 control 才创建/复用唯一 PR、设置标签并请求审核；
5. 用户在 GitHub 提交 review、inline comment 或对话评论；
6. Event Router 持久化反馈，settle 后 Judge 选择答复、修订或等待等动作。answer 和 revise 都派发到原 Worker；answer 须确认本地提交和远端 PR head 未改动。授权 Request Changes 经 grace 可直达修订，见 [Review 循环](review-loop.md)；
7. revise 由同一 worker 恢复 Codex 会话并 push，control 核验交付、处理线程，无未决反馈时重新请求审核；
8. 用户根据仓库自身 CI 和 review 在网页人工 Approve + Merge；
9. Webhook 或对账确认当前 PR 已合并后，control 在同一事务中记录合并、成功归档、停止请求和通知意图；
10. 原 Worker 确认执行已退出、远端操作结清后释放工作区绑定。GitHub 通知独立恢复；下一 INITIAL 独占目录后清理旧副本。原 Worker 离线或通知失败都不撤销归档，见[归档与资源回收](../operations/upgrade.md#合并归档与资源回收分离)。

## 6. 对账、取消与会话查询

- **PR / 反馈对账**：独立循环读取已追踪 PR 的 merge/close/reopen，以及 Issue/PR 普通评论、
  review 正文和行内线程；持久化分页进度和退避。未合并关闭进入废弃收尾，重开不自动恢复；对账不补建漏失的新 Flow trigger，不追溯执行旧评论命令。
- **跨轮反馈**：恢复后，以本轮首个 INITIAL 的持久创建时间为界；同秒、更早或缺少创建时间的原 Issue 评论不进入新批次，编辑旧评论也不会改变归属。新 PR 上的评论和 Review 无此时间限制。普通反馈每次每来源至多 50 条，线程每次至多 20 条评论；不以分页片段判定线程可以 resolve。
- **取消**：事务内记录结束本轮和停止意图，阻止新派发；worker 取消
  执行并回收持有的进程组，journal 保存停止凭据。control 确认凭据且已发送 effects 收敛后
  才释放绑定；未知旧进程继续阻塞。取消不回滚已发生的远端操作，合并事实仍进入原归档路径。
- **Drain**：持久标记 worker 不接新 Flow；已绑定 Flow 的修订/归档仍继续，不隐式取消业务。
- **Projects**：独立投影循环把本地业务状态同步到可选组织看板，失败不改变 Flow。
- **会话历史**：HTTP 请求经 control 验证 Flow/session 身份，通过现有 WS 查询原 worker，
  control 负责跨 session 游标与可信归属，不把全文存入 PostgreSQL。
  worker.history 由 WorkerBusinessCallbacks 注册，请求、回复和验证拒收均走共同 FIFO；
  慢业务回调与 history 互相等待，不另设查询旁路。

依据：[PR 对账](../../app/controler/reconciliation/pull_requests.py)、
[反馈对账](../../app/controler/reconciliation/feedback.py)、
[取消与停机](../operations/cancel-drain.md)、[Projects 同步](../../app/controler/reconciliation/projects.py)、
[会话查询](conversation-api.md)。冲突自动处理、跨 worker 迁移和每 Flow 动态强隔离仍未实现；
真实部署与故障恢复待验收，具体边界见[组件职责](components.md)和[已知短板](decisions.md)。

## 7. 流程中的 Flow 事实

每一步在改变 Flow 状态时，同一事务内追加一条事实，`kind` 即 `FlowEvent` 的值；
设计见 [Flow 事实账本与状态表](flow-ledger.md)。

| 步骤 | 触发 | 事实 `kind` | Flow 状态 |
|---|---|---|---|
| 1–2 | 标签触发或控制台创建 | `created` | `queued` |
| 3 | Job 派发给 Worker | `job_claimed` | `running` |
| 3 | 命名推理结果应用 | `titled` | 不变 |
| 4 | INITIAL 结果核验并应用 | `delivery_applied` | `review` |
| 5–6 | 反馈批次创建修订 Job | `job_enqueued` | `queued` |
| 7 | 修订 Job 派发、结果应用 | `job_claimed`，随后 `delivery_applied` | `review` |
| 9 | 确认当前 PR 合并 | `merge`、`archive` | `archived` |
| 10 | 原 Worker 停止、释放与通知收尾 | 不再重复归档 | `archived` |

异常与运维路径：

| 情形 | 事实 `kind` | Flow 状态 |
|---|---|---|
| 未授权派发的任务租约到期或被拒绝 | `assignment_requeued` | `queued` |
| 已启动的执行无法确认结果 | `execution_uncertain` | `blocked` |
| 结果失败或核验未通过 | `result_failed` | `blocked` |
| PR 来源分支被改动 | `delivery_baseline_rejected` | `blocked` |
| 显式 retry | `retry_requested` | `queued`，已有归档结论时 `finalizing` |
| 取消、结束本轮、`/flow stop`、未合并关闭 PR | `finalization_requested`，收尾完成后 `archive` | `archived` |
| 显式恢复新轮次 | `round_resumed` | `queued` |
| 归档后发现此前已合并 | `merge`，必要时 `archive_correction` | 不变 |

Job、交付和反馈批次自身的变化不写 Flow 事实；事件名说明 Flow 为什么变化，细节仍在对应的表里。
