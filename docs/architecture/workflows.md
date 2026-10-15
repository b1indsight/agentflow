# 端到端流程

## 当前自动化覆盖的切片

```text
签名正确的 Webhook
  → FastAPI 持久化 webhook inbox 并返回 202
  → 后台消费者 → Event Router 去重 / 授权 / 写 PostgreSQL
  → flow + queued job
  → serve 通过认证 WebSocket 向公平选中的空闲 worker assign
  → FlowExecution 固定单槽 worker/repo，prepare 读取真实 Issue/default branch
  → Started / Heartbeat；INITIAL 获得临发 scoped push capability
  → Codex commit/push → worker-local flow.open_pr MCP → control tool ledger
  → worker 核验本地 VCS/remote；Result 与持久 tool outcome 核对
  → control 再核对 GitHub ref → find-or-open 唯一 PR → ResultAccepted；保留 ACTIVE 与 binding
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
6. Event Router 持久化反馈，settle 后由 control 的 Judge 作前置判断；只有 revise 创建 revision job，
   answer/ignore/wait 直接处理批次。授权 Request Changes 经 grace 直达修订；
7. revise 由同一 worker 恢复 Codex 会话并 push，control 核验交付、处理线程，无未决反馈时重新请求审核；
8. 用户根据仓库自身 CI 和 review 在网页人工 Approve + Merge；
9. `pull_request.closed (merged=true)` 触发 MERGED 和 archive job；
10. worker 清理工作区，control 归档记录并更新 GitHub 状态。

步骤 3–7 已实现并合并，具备隔离自动化覆盖，真实 GitHub App/Codex/bub E2E 待执行。
步骤 8 由用户人工完成；M3 F1 已实现步骤 9 的原子合并与后续任务收敛，迟到结果不能恢复编码。
步骤 10 已接入 F2 原 worker 清理凭据与 F3 状态回写：Issue/PR 增量更新标签、各一条固定归档说明，
回写失败保留 ACTIVE Flow 和原 binding，成功后才原子完成并 ACK。真实 M3 闭环待验收，见
[F3 实施与验收](../plan/m3-archive-status.md)。

## 6. 对账、取消与旁路查询

- **PR / 反馈对账**：独立循环读取已追踪 PR 的 merge/close/reopen，以及 Issue/PR 普通评论、
  review 正文和行内线程；持久化分页进度和退避。close 保留绑定并暂挂，reopen 核验基线后
  恢复；对账不补建漏失的新 Flow trigger，不追溯执行旧评论命令。
- **取消**：事务内保存 CANCELLED 和 stop intent，阻止新派发与 GitHub 写操作；worker 取消
  执行并回收持有的进程组，journal 保存停止凭据。control 确认凭据且已发送 effects 收敛后
  才释放绑定；未知旧进程继续阻塞。取消不回滚已发生的远端操作，合并事实仍进入原归档路径。
- **Drain**：持久标记 worker 不接新 Flow；已绑定 Flow 的修订/归档仍继续，不隐式取消业务。
- **Projects**：独立投影循环把本地业务状态同步到可选组织看板，失败不改变 Flow。
- **会话历史**：HTTP 请求经 control 验证 Flow/session 身份，通过现有 WS 查询原 worker，
  control 负责跨 session 游标与可信归属，不把全文存入 PostgreSQL。

依据：[PR 对账](../../app/controler/reconciliation/pull_requests.py)、
[反馈对账](../../app/controler/reconciliation/feedback.py)、
[取消与停机](../operations/cancel-drain.md)、[Projects 同步](../../app/controler/reconciliation/projects.py)、
[会话查询](conversation-api.md)。冲突自动处理、跨 worker 迁移和每 Flow 动态强隔离仍未实现；
真实部署与故障恢复待验收，具体边界见[组件职责](components.md)和[已知短板](decisions.md)。
