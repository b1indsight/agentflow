# 端到端流程

1. 你建 issue、打 `flow` 标
2. webhook `issues.labeled`
3. 建工作区、codex 首轮编码
4. 推分支、开 PR（`Closes #N`）、@你审核
5. 你在 GitHub 上审
6. webhook 回传，汇总反馈 resume codex 修订
7. 再推、re-request review（5–7 循环）
8. 你在网页上 Approve + 点 Merge（工具不参与合并；CI 由仓库自身 CI 呈现在 PR 上，你据此判断）
9. webhook `pull_request.closed (merged=true)` → 记录合并、issue 被 `Closes #` 自动关闭
10. 归档、清理工作区、Project 卡片移 Done

