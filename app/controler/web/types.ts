export type Phase = "queued" | "running" | "review" | "blocked" | "done";
export type Tab = "overview" | "conversation" | "jobs";
export interface FlowSummary {
  id: number;
  repo: string;
  issue_number: number;
  issue_url: string;
  title: string | null;
  pr_number: number | null;
  pr_url: string | null;
  lifecycle: string;
  phase: Phase;
  action: string;
  worker_id: string | null;
  worker_historical: boolean;
  revision_cycles: number;
  branch: string | null;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  state_version: number;
}
export interface SourceStatus {
  source: string;
  status: "pending" | "syncing" | "available" | "partial" | "unavailable";
  reason: string | null;
  last_success_at: string | null;
  limitations: string[];
}
export interface FlowDetail extends FlowSummary {
  reason: string;
  next_condition: string;
  sources: SourceStatus[];
}
export interface FlowPage {
  items: FlowSummary[];
  counts: Record<Phase, number>;
  next_cursor: string | null;
  has_more: boolean;
}
export interface RepositoryPage {
  items: string[];
  next_cursor: string | null;
  has_more: boolean;
}
export interface JobSummary {
  id: number;
  flow_id: number;
  attempt: number;
  kind: string;
  state: string;
  worker_id: string | null;
  started_at: string | null;
  updated_at: string;
  created_at: string;
  finished_at: string | null;
  error_code: string | null;
}
export interface JobPage {
  items: JobSummary[];
  next_cursor: string | null;
  has_more: boolean;
}
export interface ActivityItem {
  key: string;
  version: number;
  source: string;
  kind: string;
  occurred_at: string;
  updated_at: string;
  observed_at: string;
  author: string;
  title: string;
  body: string;
  body_length: number;
  url: string | null;
  parent_key: string | null;
  deleted: boolean;
  prior: boolean;
  previous_version: number | null;
  metadata: Record<string, unknown>;
}
export interface ActivityPage {
  items: ActivityItem[];
  snapshot: number;
  total: number;
  next_cursor: string | null;
  has_more: boolean;
  sources: SourceStatus[];
  changes: Record<string, number>;
}
export interface ContentPage {
  key: string;
  version: number;
  text: string;
  next_offset: number | null;
  total: number;
  previous_version: number | null;
}
