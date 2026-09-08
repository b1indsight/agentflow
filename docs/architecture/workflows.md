# 端到端流程

## 当前自动化覆盖的切片

```text
签名正确的 Webhook
  → FastAPI 后台交接并返回 202
  → Event Router 去重 / 授权 / 写 PostgreSQL
  → flow + queued job
  → serve 通过认证 WebSocket 向公平选中的空闲 worker assign
  → FlowExecution 固定单槽 worker/repo，prepare 读取真实 Issue/default branch
  → Started / Heartbeat；INITIAL 获得临发 scoped push capability
  → Codex commit/push → worker-local flow.open_pr MCP → control tool ledger
  → worker 核验本地 VCS/remote；Result 与持久 tool outcome 核对
  → control 再核对 GitHub ref → find-or-open 唯一 PR → ResultAccepted；保留 ACTIVE 与 binding
  → review/comment → 持久反馈与 settle → 冻结下一批
  → Judge 判定 revise/answer/wait/ignore；授权 Request Changes 经 grace 直达 revise
  → revise：同 repo/branch/PR resume 或一次性 session 兜底 → 双端交付核验
  → 原线程说明 → 覆盖与版本检查 → 保守 resolve 并复读 → 无未决反馈时请求 re-review
  → answer/ignore 精确消费本批版本；wait 保留问题，等待新反馈
  → 断线：unstarted 等 lease 后重排；started 只由原 worker 在 grace 内续跑/重放结果
  → serve 重启：恢复持久 assignment/result inbox，不重复派发 started job
```

测试覆盖内部 Issue→PR、M2 批次和修订控制流；GitHub/Codex 使用替身，bub 契约连接假 provider。
真实服务闭环尚未验收；同秒重复 webhook 重放仍有已知缺陷，见 [M2 状态与待办](README.md#m2-状态与待办)。

## 5. 目标闭环

1. 用户创建 issue 并添加 `flow` 标签；
2. GitHub 发送 `issues.labeled` Webhook；
3. control 派发 INITIAL task，worker 建立隔离工作区并启动 Codex 首轮编码；
4. Code Agent commit/push 后调用 `flow.open_pr`；worker 与 control 依次核验远端 SHA，control 创建带
   `Closes #N` 的 PR 并请求审核；
5. 用户在 GitHub 提交 review、inline comment 或对话评论；
6. Event Router 持久化反馈，settle 后由 Judge 判定 revise/wait/answer/ignore；授权 Request Changes 经 grace 直达修订；
7. revise 由同一 worker 恢复 Codex 会话并 push，control 核验交付、处理线程，无未决反馈时重新请求审核；
8. 用户根据仓库自身 CI 和 review 在网页人工 Approve + Merge；
9. `pull_request.closed (merged=true)` 触发 MERGED 和 archive job；
10. worker 清理工作区，control 归档记录并更新 GitHub 状态。

步骤 3–7 已实现并合并，具备隔离自动化覆盖，真实 GitHub App/Codex/bub E2E 待执行。
步骤 8 由用户人工完成；M3 F1 已实现步骤 9 的原子合并与后续任务收敛，迟到结果不能恢复编码。
步骤 10 当前保留健康 worker 的清理骨架；可靠清理/异常恢复及状态回写分别等待 M3 F2/F3。
冲突处理、取消运行中进程、Project 更新与通用 reconcile 尚未完成；具体边界见[组件职责](components.md)和[已知短板](decisions.md)。
