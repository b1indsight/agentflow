export type Phase = "queued" | "running" | "review" | "blocked" | "done";
export type Tab = "overview" | "conversation" | "jobs";
export interface FlowSummary {
  id: number;
  issue: number;
  title: string;
  repo: string;
  phase: Phase;
  worker: string | null;
  pr: number | null;
  cycles: number;
  age: string;
  note?: string;
}
export interface Flow extends FlowSummary {
  branch: string | null;
  action: string;
  reason: string;
  nextCondition: string;
  records: { label: string; kind: string; status: string; note: string }[];
  progress: { title: string; description: string }[];
}
export type Block =
  | { kind: "paragraph" | "heading" | "verdict"; text: string }
  | { kind: "tool"; name: string; output: string; status: string }
  | {
      kind: "thread";
      path: string;
      lines: string;
      code: string;
      replies: { author: string; time: string; text: string }[];
      resolved: boolean;
    };
export interface Activity {
  id: string;
  time: string;
  source: string;
  author: string;
  kind: "github" | "agent" | "event";
  blocks: Block[];
}
