/** Preview-only fixtures. No network, credentials, storage, or business mutations. */
import type { Activity, Block, Flow, FlowSummary } from "./types";

const summaries: FlowSummary[] = [
  {
    id: 37,
    issue: 88,
    title: "补全部署与接入文档",
    repo: "agentflow",
    phase: "queued",
    worker: null,
    pr: null,
    cycles: 0,
    age: "12 分钟前",
  },
  {
    id: 41,
    issue: 91,
    title: "增加任务执行超时提示",
    repo: "agentflow",
    phase: "queued",
    worker: null,
    pr: null,
    cycles: 0,
    age: "8 分钟前",
  },
  {
    id: 42,
    issue: 82,
    title: "为 Webhook 接收添加退避重试",
    repo: "agentflow",
    phase: "running",
    worker: "worker-1",
    pr: 95,
    cycles: 2,
    age: "刚刚",
  },
  {
    id: 44,
    issue: 90,
    title: "完善会话分页与历史读取",
    repo: "agentflow",
    phase: "running",
    worker: "worker-2",
    pr: 97,
    cycles: 1,
    age: "3 分钟前",
  },
  {
    id: 38,
    issue: 76,
    title: "简化配置加载与校验",
    repo: "agentflow",
    phase: "review",
    worker: "worker-3",
    pr: 89,
    cycles: 1,
    age: "24 分钟前",
  },
  {
    id: 40,
    issue: 85,
    title: "补充 Worker 恢复状态检查",
    repo: "agentflow",
    phase: "blocked",
    worker: "worker-4",
    pr: 94,
    cycles: 2,
    age: "46 分钟前",
    note: "Worker 离线，等待原执行恢复",
  },
  {
    id: 39,
    issue: 79,
    title: "增加服务运行指标",
    repo: "agentflow",
    phase: "done",
    worker: "worker-2",
    pr: 90,
    cycles: 1,
    age: "2 小时前",
  },
  {
    id: 45,
    issue: 12,
    title: "整理脚本的错误输出",
    repo: "toolkit",
    phase: "review",
    worker: "worker-5",
    pr: 14,
    cycles: 0,
    age: "35 分钟前",
  },
];

// These are sample facts, not a second implementation of the server's phase rules.
export const flows: Flow[] = summaries.map((f) => {
  const [action, reason, nextCondition] = {
    queued: [
      "等待分配",
      "任务已接收，尚未分配执行资源。",
      "可用 Worker 接收任务后，开始首轮编码。",
    ],
    running: [
      f.cycles ? `第 ${f.cycles} 轮修订` : "首轮编码",
      `${f.worker} 正在${f.cycles ? "根据审核反馈修订代码并验证" : "进行首轮编码与验证"}。`,
      "本轮执行结束并更新 PR 后，进入待审核。",
    ],
    review: [
      "等待审核",
      `PR #${f.pr} 已交付，等待 GitHub 上的审核结果。`,
      "收到修改反馈后继续修订；PR 合并后进行归档。",
    ],
    blocked: [
      "等待执行恢复",
      "Worker 离线，等待原执行恢复",
      "等待原 Worker 恢复，并确认原执行状态后继续。",
    ],
    done: [
      "归档完成",
      "PR 已合并，工作区清理和归档已完成。",
      "流程已结束，无需等待后续执行。",
    ],
  }[f.phase];
  return {
    ...f,
    branch: f.phase === "queued" ? null : `flow/issue-${f.issue}`,
    action,
    reason,
    nextCondition,
    records:
      f.phase === "queued"
        ? []
        : [
            ...(f.phase === "done"
              ? [
                  {
                    label: "归档清理",
                    kind: "ARCHIVE",
                    status: "已完成",
                    note: "工作区清理与归档回写完成",
                  },
                ]
              : []),
            ...(f.cycles
              ? [
                  {
                    label: `第 ${f.cycles} 轮修订`,
                    kind: "REVISION",
                    status:
                      f.phase === "running"
                        ? "执行中"
                        : f.phase === "blocked"
                          ? "等待恢复"
                          : "已完成",
                    note: "根据审核反馈继续原会话，修订后重新交付",
                  },
                ]
              : []),
            {
              label: "首轮编码",
              kind: "INITIAL",
              status: f.phase === "running" && !f.cycles ? "执行中" : "已完成",
              note: "首轮编码与 PR 交付",
            },
          ],
    progress:
      f.phase === "queued"
        ? []
        : [
            ...(f.phase === "done"
              ? [
                  {
                    title: "已完成归档",
                    description: "PR 合并后清理工作区，释放 Worker",
                  },
                ]
              : []),
            ...(f.phase === "blocked"
              ? [
                  {
                    title: "等待执行恢复",
                    description: "原 Worker 离线；恢复确认前保留绑定",
                  },
                ]
              : []),
            ...(f.cycles
              ? [{ title: "修订反馈", description: "基于审核反馈继续原会话" }]
              : []),
            ...(f.pr
              ? [
                  {
                    title: "PR 已创建",
                    description: `交付到 PR #${f.pr}，等待反馈`,
                  },
                ]
              : []),
            { title: "首轮执行开始", description: `${f.worker} 接收任务` },
            { title: "任务已接收", description: `Issue #${f.issue} 触发 Flow` },
          ],
  };
});

const paragraph = (text: string): Block => ({ kind: "paragraph", text });
const heading = (text: string): Block => ({ kind: "heading", text });
const tool = (name: string, output: string, status: string): Block => ({
  kind: "tool",
  name,
  output,
  status,
});

function activities(f: Flow, updated: boolean): Activity[] {
  const entries: Activity[] = [];
  const add = (
    id: string,
    time: string,
    source: string,
    author: string,
    blocks: Block[],
    kind: Activity["kind"] = "github",
  ) => entries.push({ id, time, source, author, blocks, kind });
  add("issue", "14:00", `GitHub · Issue #${f.issue}`, "b1indsight", [
    heading(f.title),
    paragraph("请处理这个任务，沿用仓库已有约定，并补充正常与失败路径的验证。"),
  ]);
  add(
    "received",
    "14:01",
    `GitHub · Issue #${f.issue} 评论`,
    "agentflow[bot]",
    [
      paragraph(
        f.phase === "queued"
          ? "任务已接收，正在等待可用 Worker。"
          : `任务已接收，交给 ${f.worker} 执行。后续交付与审核会在这里同步。`,
      ),
    ],
  );
  if (f.phase !== "queued") {
    add(
      "issue-comment",
      "14:02",
      `GitHub · Issue #${f.issue} 评论`,
      "b1indsight",
      [paragraph("补充一下：请尽量控制修改范围，并在 PR 中说明如何验证。")],
    );
    add(
      "initial",
      "14:03",
      `Agent · ${f.worker}`,
      "Codex",
      [
        paragraph("我先检查相关实现和测试，确认现有行为。"),
        tool(
          "exec_command · 读取代码",
          'rg -n "retry|backoff" app tests\n\napp/controler/events/webhooks.py\ntests/test_webhook_inbox.py',
          "2 个相关文件",
        ),
        paragraph("接下来会复用现有恢复逻辑，再检查边界条件。"),
      ],
      "agent",
    );
    add("pr", "14:12", `GitHub · PR #${f.pr} 描述`, "agentflow[bot]", [
      heading(f.title),
      paragraph(`关联 Issue #${f.issue}。本次交付包含实现调整和回归验证。`),
    ]);
    add(
      "commit",
      "14:13",
      "GitHub",
      "agentflow[bot]",
      [paragraph(`推送了首轮提交到 PR #${f.pr}`)],
      "event",
    );
    add(
      "checks",
      "14:14",
      "GitHub",
      "checks",
      [paragraph("检查通过 2 / 2 · 测试与代码检查")],
      "event",
    );
    add("review", "14:18", `GitHub · PR #${f.pr} Review`, "reviewer", [
      { kind: "verdict", text: "要求修改" },
      paragraph("整体方向可以。请补充失败后再次执行的情况，避免重复处理。"),
      {
        kind: "thread",
        path: "app/controler/events/webhooks.py",
        lines: "L42–44",
        code: "42  if should_retry(result):\n43      schedule_retry(event)\n44      return",
        resolved: false,
        replies: [
          {
            author: "reviewer",
            time: "14:19",
            text: "这里重复收到相同事件时，是否会再次调度？请加一个回归用例。",
          },
          {
            author: "agentflow[bot]",
            time: "14:20",
            text: "已收到，会在下一轮修订中检查去重与恢复路径。",
          },
        ],
      },
    ]);
    add(
      "review-input",
      "14:21",
      "Agent · 审核输入",
      "AgentFlow",
      [
        tool(
          "发送给 Agent 的输入 · 关联上方 Review",
          `请继续处理 PR #${f.pr}。审核意见来自 reviewer：补充重复事件与失败恢复的回归验证。\n\n这是从上方 GitHub 审核整理的输入，未产生一条新的 GitHub 评论。`,
          "展开查看",
        ),
      ],
      "agent",
    );
    add(
      "revision",
      "14:24",
      `Agent · ${f.worker}`,
      "Codex",
      [
        paragraph("已定位重复事件的处理路径，正在补充回归验证。"),
        tool(
          "exec_command · 运行测试",
          "uv run pytest tests/test_webhook_inbox.py -q\n\n..................\n18 passed in 1.24s",
          "18 passed",
        ),
      ],
      "agent",
    );
    add("pr-comment", "14:26", `GitHub · PR #${f.pr} 评论`, "agentflow[bot]", [
      paragraph("已更新修订提交，补充了重复事件和恢复路径的验证说明。"),
    ]);
    add(
      "revision-checks",
      "14:27",
      "GitHub",
      "checks",
      [paragraph("修订提交检查通过 2 / 2")],
      "event",
    );
  }
  if (f.phase === "done") {
    add("approved", "14:28", `GitHub · PR #${f.pr} Review`, "reviewer", [
      paragraph("审核通过，补充的验证覆盖了之前提出的边界情况。"),
    ]);
    add(
      "merged",
      "14:29",
      "GitHub",
      "b1indsight",
      [paragraph(`合并了 PR #${f.pr}，关闭 Issue #${f.issue}`)],
      "event",
    );
    add(
      "archived",
      "14:30",
      "AgentFlow",
      "system",
      [paragraph("工作区清理完成，任务已归档")],
      "event",
    );
  }
  if (updated && f.phase !== "done") {
    add(
      "fresh-comment",
      "14:32",
      `GitHub · ${f.pr ? `PR #${f.pr}` : `Issue #${f.issue}`} 评论`,
      "b1indsight",
      [
        paragraph(
          "补充一个验证点：请确认服务重启后，同一个事件仍然不会被重复处理。",
        ),
      ],
    );
    if (f.phase !== "blocked" && f.phase !== "queued")
      add(
        "fresh-agent",
        "14:33",
        `Agent · ${f.worker}`,
        "Codex",
        [paragraph("已收到补充说明，我会把服务重启场景一起验证。")],
        "agent",
      );
  }
  return entries;
}

const refreshed = new Set<number>();

/**
 * Simulate bounded activity I/O for the preview, including cancellation on navigation.
 * The offset cursor here is NOT a proposed server cursor or a claim about GitHub ordering.
 */
export async function readActivity(
  flowId: number,
  {
    cursor,
    refresh,
    signal,
  }: { cursor: string | null; refresh: boolean; signal: AbortSignal },
) {
  const flow = flows.find((f) => f.id === flowId);
  if (!flow) throw new Error("找不到这个示例 Flow");
  const offset = cursor === null ? 0 : Number(cursor);
  if (!Number.isSafeInteger(offset) || offset < 0)
    throw new Error("示例读取位置无效，请重新打开会话");
  signal.throwIfAborted();
  await new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve(null);
    }, 600);
    signal.addEventListener("abort", abort, { once: true });
  });
  signal.throwIfAborted();
  if (refresh) refreshed.add(flowId);
  const entries = activities(flow, refreshed.has(flowId));
  if (offset > entries.length)
    throw new Error("示例读取位置已失效，请重新打开会话");
  const items = entries.slice(offset, offset + 4);
  return {
    items,
    cursor: String(offset + items.length),
    hasMore: offset + items.length < entries.length,
  };
}
