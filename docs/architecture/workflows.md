# 端到端流程

## M0 已验证切片

```text
签名正确的 Webhook
  → FastAPI 后台交接并返回 202
  → Event Router 去重 / 授权 / 写 SQLite
  → flow + queued job
  → Worker claim / heartbeat / dispatch
  → job DONE 或 FAILED；重启时恢复 stale running job
```

测试使用 fake engine 或 fake 外部端口，不访问 GitHub、Codex 或 bub。这个切片证明入口、持久化和
worker 基础，不证明下面的目标闭环。

## 5. 目标闭环

1. 用户创建 issue 并添加 `flow` 标签；
2. GitHub 发送 `issues.labeled` Webhook；
3. Orchestrator 建立隔离工作区并启动 Codex 首轮编码；
4. Code Agent commit/push，Orchestrator 创建带 `Closes #N` 的 PR 并请求审核；
5. 用户在 GitHub 提交 review、inline comment 或对话评论；
6. Event Router 持久化反馈，settle 后由 Judge 判定 revise/wait/answer/ignore；
7. revise 恢复同一 Codex 会话，完成修改并 push，Orchestrator 处理线程并重新请求审核；
8. 用户根据仓库自身 CI 和 review 在网页人工 Approve + Merge；
9. `pull_request.closed (merged=true)` 触发 MERGED 和 archive job；
10. Orchestrator 归档记录、清理工作区并更新 GitHub 状态。

步骤 3–10 的控制流大部分已写入 `FlowEngine`，但真实 GitHub/Codex/bub、push、线程映射、冲突处理、
取消运行中进程、Project 更新与 reconcile 尚未完成。具体状态见[组件职责](components.md)和
[已知短板](decisions.md)。
