import type { readActivity as previewReadActivity } from "../fixtures";
import type { Activity } from "../types";

const refreshed = new Set<number>();

/** Deterministic test I/O; production continues to use the preview reader unchanged. */
export const readActivity: typeof previewReadActivity = async (
  flowId,
  { cursor, refresh, signal },
) => {
  signal.throwIfAborted();
  const offset = Number(cursor ?? 0);
  if (refresh) refreshed.add(flowId);
  const total = refreshed.has(flowId) ? 302 : 300;
  const items: Activity[] = Array.from(
    { length: Math.min(4, total - offset) },
    (_, index) => ({
      id: `history-${offset + index}`,
      time: String(offset + index),
      source: `测试 Flow ${flowId}`,
      author: "Agent",
      kind: "agent",
      blocks: [
        { kind: "paragraph", text: `历史交互 ${offset + index}` },
        {
          kind: "tool",
          name: `工具 ${offset + index}`,
          output: "输出",
          status: "完成",
        },
      ],
    }),
  );
  return {
    items,
    cursor: String(offset + items.length),
    hasMore: offset + items.length < total,
  };
};
