# 端到端流程

## 当前已验证切片

```text
签名正确的 Webhook
  → FastAPI 后台交接并返回 202
  → Event Router 去重 / 授权 / 写 PostgreSQL
  → flow + queued job
  → serve 通过认证 WebSocket 向公平选中的空闲 worker assign
  → Started / Heartbeat / Result / ResultAccepted
  → Started 固定 attempt affinity → job DONE；断线时按是否 started 执行 requeue 或 fail closed
```

测试使用 fake coordinator/executor 或 fake 外部端口，不访问 GitHub、Codex 或 bub。这个切片证明入口、
持久化和远程边界，不证明下面的目标闭环。

## 5. 目标闭环

1. 用户创建 issue 并添加 `flow` 标签；
2. GitHub 发送 `issues.labeled` Webhook；
3. control 派发 INITIAL task，worker 建立隔离工作区并启动 Codex 首轮编码；
4. Code Agent commit/push，worker 回传 result，control 创建带 `Closes #N` 的 PR 并请求审核；
5. 用户在 GitHub 提交 review、inline comment 或对话评论；
6. Event Router 持久化反馈，settle 后由 Judge 判定 revise/wait/answer/ignore；
7. revise 由同一 worker 恢复 Codex 会话并 push，control 处理线程并重新请求审核；
8. 用户根据仓库自身 CI 和 review 在网页人工 Approve + Merge；
9. `pull_request.closed (merged=true)` 触发 MERGED 和 archive job；
10. worker 清理工作区，control 归档记录并更新 GitHub 状态。

步骤 3–10 的控制流已拆入 `JobCoordinator` 与 `WorkerExecutor`，但真实 GitHub/Codex/bub、push、线程映射、冲突处理、
取消运行中进程、Project 更新与 reconcile 尚未完成。具体状态见[组件职责](components.md)和
[已知短板](decisions.md)。
