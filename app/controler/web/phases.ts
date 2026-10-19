import type { Phase } from "./types";

/** Shared display order and labels, independent of the preview data source. */
export const phases: readonly (readonly [Phase, string])[] = [
  ["queued", "排队中"],
  ["running", "执行中"],
  ["review", "待审核"],
  ["blocked", "受阻"],
  ["done", "已归档"],
];

/** Display-only archive grouping must not imply a closed PR was successfully merged. */
export function actionLabel(action: string) {
  if (action === "delivery_closed") return "已关闭 · 未合并";
  if (action === "archived") return "归档完成";
  return action;
}
