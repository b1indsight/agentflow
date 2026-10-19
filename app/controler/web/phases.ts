import type { FlowSummary, Phase } from "./types";

/** Shared display order and labels, independent of the preview data source. */
export const phases: readonly (readonly [Phase, string])[] = [
  ["queued", "排队中"],
  ["running", "执行中"],
  ["review", "待审核"],
  ["blocked", "受阻"],
  ["done", "已归档"],
];

/** Display archives must preserve the distinction between cancellation, closure and success. */
export function actionLabel({
  action,
  archived_at,
}: Pick<FlowSummary, "action" | "archived_at">) {
  if (action === "cancelled") return archived_at ? "已取消 · 已归档" : "已取消";
  if (action === "delivery_closed") return "已关闭 · 未合并";
  if (action === "archived") return "归档完成";
  return action;
}
