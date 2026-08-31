# Review 修订循环

本页定义 M1+ 的目标语义，并单独列出 M0 当前实现；除“当前实现”明确说明的部分外，不代表已经完成
真实 GitHub/Codex/bub 验证。

## B.1 反馈入口

目标上，普通非 bot 反馈先进入持久化缓冲，再由 Judge 决定是否改码：

```text
非 bot 反馈 → settle 静默窗口 → Judge
        ├ revise → Codex 修订
        ├ answer → Judge 作答，不改码
        ├ wait   → 保留反馈，等待更多上下文
        └ ignore → 略过
```

目标架构保留两类强信号直达修订：显式 `@flowtool` 指令，以及 GitHub Request Changes review。
它们跳过 Judge，但仍应先持久化反馈、遵守同 flow 串行约束并由 revision job 执行，避免在 Webhook
请求内启动长任务。

当前 router 已实现：忽略 bot、忽略 approve、记录 review body / inline / PR conversation、按 flow
重置内存 settle timer，并在 timer 到点后创建持久化 TRIAGE job。Request-changes 只把等待缩短为
`submit_grace_seconds`，不会绕过 Judge；`@flowtool` 也没有专用路由。因此上述强信号例外仍是 M1+
目标，而不是 M0 当前能力。

当前 timer 本身不持久化，进程重启会丢失尚未到点的 settle；同 flow 串行锁和 `/flow restart` 也尚未实现。

## B.2 Judge 结构化产出

```json
{
  "verdict": "revise | wait | answer | ignore",
  "confidence": 0.0,
  "reason": "判断依据",
  "instruction": "revise 时的具体改动",
  "answer": "answer 时的回复",
  "open_points": ["wait 时仍未决定的问题"]
}
```

JobCoordinator 仅在 `verdict=revise` 且 `confidence >= FLOWTOOL_CONFIDENCE_THRESHOLD` 时创建 revision job；
answer 创建 answer job；wait/ignore 不执行外部动作。Bub 调用仍未校准，解析失败保守返回 wait。

## B.3 安全兜底

已实现的兜底：bot 忽略、allowlist 触发、置信度阈值、Judge 解析失败→wait、最大修订轮数和单次
Codex timeout。

目标但未实现：revision 前可中止窗口、`/flow restart`、运行中 kill、预算限制、同 flow 串行化、
外部 push 检测和更强的 prompt-injection 隔离。人工合并仍是最后安全门。

## B.4 Revision job

当前顺序：

1. 读取当前 attempt 的全部 `relayed=false` 反馈；
2. 用 Judge instruction、反馈正文、inline path/diff hunk/original commit id 和 VCS 指引构造 prompt；
3. `codex exec resume <session_id> ... --output-schema`；
4. 非零 exit 将 flow 标为 FAILED，不标记反馈 relayed；
5. 成功时解析 `CodexResult`，处理 addressed/deferred/clarification/summary，再标记反馈 relayed；
6. flow 回到 PR_OPEN，设置 `flow:in-review` 并重新请求 allowlist 用户审核。

`CodexResult`：

```json
{
  "summary": "...",
  "addressed": [{"comment_id": 123, "note": "..."}],
  "deferred": [{"comment_id": 456, "reason": "..."}],
  "needs_clarification": ["..."]
}
```

重要边界：当前 `_thread_id_for()` 是桩，所以 addressed 只能更新本地 resolved 标记，不能真实调用
GitHub resolve；当前代码也不会把 addressed note 发回线程。clarification 不会暂停 re-review；Codex 是否
完成 commit/push 仅由 exit code 间接假定，没有远端校验、rebase 或冲突检测。

## B.5 并发目标

目标是每 flow 至多一个 Codex run，运行期间的新反馈留给下一批，不打断当前 run；不同 flow 可并行。
当前 serve 允许多个单槽 worker 并行消费不同 flow，并以 attempt affinity 防止同一会话跨 worker；尚未实现
显式 per-flow 锁或 `/flow restart`。现有 job/attempt 状态约束会阻止一个 attempt 被两个 worker 同时领取。

## B.6 目标内部状态

```text
        非 bot 反馈        settle + Judge=revise
IDLE ───────────► SETTLING ─────────────────► RUNNING
 ▲                    │                         │
 └──── 完成/等待 ─────┴──── 新反馈留到下一批 ──┘
```

这些 IDLE/SETTLING/RUNNING 是设计层的子状态，当前数据库没有对应枚举；当前持久化状态仍是
`PR_OPEN ⇄ REVISING`，settle timer 在内存中。

## B.7 当前参数

| 环境变量 | 默认 | 当前用途 |
|---|---:|---|
| `FLOWTOOL_SETTLE_SECONDS` | 75 | 普通反馈后的静默窗口 |
| `FLOWTOOL_SUBMIT_GRACE_SECONDS` | 15 | review submitted 后的缩短窗口 |
| `FLOWTOOL_CONFIDENCE_THRESHOLD` | 0.6 | revise 的最低 Judge 置信度 |
| `FLOWTOOL_CODEX_RUN_TIMEOUT_SECONDS` | 1200 | 单次 Codex timeout |
| `FLOWTOOL_MAX_REVISION_CYCLES` | 20 | 每 attempt 的最大修订轮数 |

## B.8 后续方向

bub 可在未来成为 GitHub PR 对话的原生 channel adapter，使 triage 与 answer 共用会话；这属于路线图，
当前 subprocess adapter 不具备该能力。
