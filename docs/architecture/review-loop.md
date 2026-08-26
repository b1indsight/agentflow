# Review 修订循环

§3 里"你审 → 回喂 codex → 再改"这段闭环的完整语义。核心：**用语义判定决定要不要改、改什么**，不靠标记或点名。

## B.1 触发不靠语法，靠语义

所有非 bot 反馈只是"进入判定的素材"，不直接触发改码。判定由 `Judge`（§4.10，MVP 用 bub）做：

```
非 bot 反馈 → 缓冲 → settle 窗口(等会话停顿) → Judge 语义判定
        ├ revise → (贴"按 X 修改中") → codex 修订
        ├ answer → bub 以小 agent 身份在 PR 作答（不改码）
        ├ wait   → 继续缓冲，更多消息来了再判
        └ ignore → 略过
```

- **忽略 bot 自己的事件**（`sender == flowtool[bot]`），否则自触发死循环。
- **Approve** 是合并侧信号（人工网页合并），不进此循环。
- `settle` 窗口（~60–90s 静默）只为"等你们把话说完再判"，不再是触发器。

## B.2 Judge 的结构化产出

```json
{
  "verdict": "revise | wait | answer | ignore",
  "confidence": 0.0,
  "reason": "判断依据",
  "instruction": "verdict=revise：综合讨论得出的、要做的具体改动",
  "answer": "verdict=answer：回给提问者的话",
  "open_points": ["verdict=wait：还没定的点"]
}
```

- `revise` 且 `confidence ≥ 阈值` → 进 codex 修订，`instruction` 是权威指令、讨论原文作证据。
- 低于阈值 / 模棱两可 → 一律 `wait`（保守，宁可不动）。

## B.3 误判兜底

1. 保守默认 `wait`；2. 置信度阈值；3. `revise` 前贴一句意图、你可 `/flow stop` 否决；
4. 低风险（合并人工，错误只落在**未合并**的 PR 分支）；
5. 显式路径仍可用但非必需（`@flowtool 改…` 或 Request changes = 强信号，直接 revise、跳过判定）。

## B.4 一次 revise run

1. flush：取未回传反馈（review 正文 + inline + 讨论），标 `relaying`
2. ContextBuilder 组 prompt：`instruction` + 每条 inline 的 `path` + `diff_hunk` 原文 + `comment_id` + `original_commit_id`
   （按**内容**定位，避开行号漂移）+ VCS 指引 + 硬约束"用本仓库 VCS 提交 + push、别碰 GitHub API"
3. `codex exec resume <sid> <prompt> --output-schema out.json -o last.txt`（run 超时 + 轮数上限）
4. 成功：codex commit + push（push 前按 VCS 指引 rebase base，冲突 → FAILED）→ **Orchestrator** 开 / 更新 PR、按产出处理线程 → 标 `relayed` → re-request review + 贴 summary + 标签 `flow:in-review`
5. codex 产出（`--output-schema`）：
   ```json
   { "summary": "...",
     "addressed": [{"comment_id": 123, "note": "怎么改的"}],
     "deferred":  [{"comment_id": 456, "reason": "为何没改"}],
     "needs_clarification": ["歧义点…"] }
   ```
   - `addressed` → GraphQL `resolveReviewThread` **自动 resolve** + 回 note（可 re-open）
   - `deferred` → 回 reason，保持 open
   - `needs_clarification` 非空 → 发 PR 评论、转 IDLE 等你回复（不 re-request）
6. 失败 / 超时：反馈退回未回传、贴说明、计入失控闸

## B.5 并发

每 flow 至多一个 codex run 在飞；run 期间来的反馈攒到下一批（**不打断**）；`/flow restart` 才 kill 重开。

## B.6 循环状态机（PR_OPEN 内部）

```
        非bot反馈           settle到点 & Judge=revise
 IDLE ───────────► SETTLING ───────────────────► RUNNING (codex)
  ▲                 │  ▲                           │      │运行中来反馈
  │ 完成&无pending   │  │新反馈:重置 / Judge=wait:留   │      ▼ 存下批
  └──────────────────┘  └─────────── 完成&有pending ─┘
  /flow restart: kill→重开        /flow stop | 去标: CANCELLED
```

## B.7 参数（默认，可调）

| 参数 | 默认 | 含义 |
|---|---|---|
| settle 静默窗口 | 60–90s | 等会话停顿再判 |
| submit grace | 15s | Request-changes 后收齐 inline |
| confidence 阈值 | 0.6 | 低于当 `wait` |
| run 超时 | 20min | 单次 codex |
| 最大修订轮数 | 20 / flow | 失控闸 |

## B.8 未来（记一笔）

bub 本是"共处对话"的 runtime——日后可把 **GitHub PR 对话做成 bub 的一个 channel**（如同 Telegram），
让 agent 成为对话原生参与者，triage 与作答同处一个 runtime。属 §9-1 / §9-4 方向。
