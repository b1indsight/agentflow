import type { Phase } from "./types";

/** Shared display order and labels, independent of the preview data source. */
export const phases: readonly (readonly [Phase, string])[] = [
  ["queued", "排队中"],
  ["running", "执行中"],
  ["review", "待审核"],
  ["blocked", "受阻"],
  ["done", "已完成"],
];
