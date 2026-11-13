import type { ArchiveReason, FlowSummary, Phase } from "./types";

/** Shared display order and labels, independent of the preview data source. */
export const phases: readonly (readonly [Phase, string])[] = [
  ["queued", "待处理"],
  ["running", "进行中"],
  ["review", "待审核"],
  ["blocked", "受阻"],
  ["archived", "已归档"],
];

export const archiveReasonLabels: Record<ArchiveReason, string> = {
  succeeded: "成功",
  abandoned: "废弃",
};

/** Primary badges use the five-column projection, never the finalizing lifecycle or job action. */
export function flowLabel({
  phase,
  archive_reason,
}: Pick<FlowSummary, "phase" | "archive_reason">): string {
  const label = phases.find(([value]) => value === phase)![1];
  return phase === "archived" && archive_reason
    ? `${label} · ${archiveReasonLabels[archive_reason]}`
    : label;
}

/** Execution details can expose finalization without claiming the round is already archived. */
export function actionLabel(flow: FlowSummary): string {
  if (flow.lifecycle === "archived") return flowLabel(flow);
  if (flow.lifecycle === "finalizing") return "正在收尾（finalizing）";
  const { action } = flow;
  if (action === "archive_blocked") return "收尾受阻";
  return action;
}
