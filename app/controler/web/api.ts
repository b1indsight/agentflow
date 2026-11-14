import { SessionCache } from "./sessionCache";
import type {
  ActivityPage,
  ArchiveReason,
  ContentPage,
  FlowDetail,
  FlowPage,
  FlowTarget,
  FlowTargetPage,
  InferenceNoticePage,
  JobPage,
  RepositoryPage,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** Same-origin API client. Responses are deliberately never replaced with fixtures. */
export async function api<T>(
  path: string,
  params: Record<string, string | number | boolean | null | undefined> = {},
  signal?: AbortSignal,
  options: RequestInit = {},
): Promise<T> {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(params))
    if (value !== null && value !== undefined && value !== "")
      url.searchParams.set(key, String(value));
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    let code = `请求失败 (${response.status})`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (typeof body.detail === "string" && body.detail) code = body.detail;
    } catch {
      // A proxy may return an empty error body; status remains actionable.
    }
    throw new ApiError(code, response.status);
  }
  return response.json() as Promise<T>;
}

const contentCache = new SessionCache<string, ContentPage>(64);
const threadCache = new SessionCache<string, ActivityPage>(12);

/** Cache only completed immutable reads; each caller keeps its own cancellation lifetime. */
async function sessionRead<T>(
  cache: SessionCache<string, T>,
  identity: (string | number | null)[],
  read: () => Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  signal?.throwIfAborted();
  const key = JSON.stringify(identity);
  const saved = cache.get(key);
  if (saved !== undefined) return saved;
  const value = await read();
  signal?.throwIfAborted();
  cache.set(key, value);
  return value;
}

export const getFlows = (
  params: Record<string, string | number | undefined>,
  signal?: AbortSignal,
) => api<FlowPage>("/api/flows", params, signal);
export const getRepos = (cursor: string | null, signal?: AbortSignal) =>
  api<RepositoryPage>("/api/repos", { cursor, limit: 100 }, signal);
export const getFlowTargets = (cursor: string | null, signal: AbortSignal) =>
  api<FlowTargetPage>("/api/flow-targets", { cursor }, signal);
export const createFlow = async (target: FlowTarget, task: string) => {
  const flow = await api<FlowDetail>("/api/flows", {}, undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...target, task_content: task }),
  });
  // An incomplete success response cannot identify the committed Flow for verification/navigation.
  if (!Number.isSafeInteger(flow.id) || flow.id <= 0)
    throw new Error("创建响应缺少有效的 Flow ID");
  return flow;
};

/** Only explicit refusals prove no Flow was created; lost responses must never be resent. */
export function createFailure(error: unknown): {
  unknown: boolean;
  message: string;
} {
  if (
    error instanceof ApiError &&
    [400, 401, 403, 404, 409, 410, 422].includes(error.status)
  )
    return {
      unknown: false,
      message: `未受理创建：${error.status === 401 ? "登录已失效，请重新登录。" : error.status === 403 ? "没有操作权限，或目标授权已失效。" : "请检查内容或重新读取项目。"}（${error.message}）`,
    };
  return {
    unknown: true,
    message:
      "无法确认 Flow 是否已创建。请先清空筛选、刷新看板核对；再次创建可能重复。此处不会自动重发。",
  };
}
export const getFlow = (id: number, signal?: AbortSignal) =>
  api<FlowDetail>(`/api/flows/${id}`, {}, signal);
export const archiveFlow = (
  id: number,
  request: {
    reason: ArchiveReason;
    summary: string;
    expected_round_no: number;
  },
) =>
  api<FlowDetail>(`/api/flows/${id}/archive`, {}, undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
export const resumeFlow = (id: number, expectedRoundNo: number) =>
  api<FlowDetail>(`/api/flows/${id}/resume`, {}, undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_round_no: expectedRoundNo }),
  });
export const retryFlow = (id: number) =>
  api<FlowDetail>(`/api/flows/${id}/retry`, {}, undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });

const retryRefusals: Record<string, string> = {
  flow_not_found: "Flow 不存在，请刷新确认。",
  retry_no_change: "当前没有可重试的任务，请刷新确认状态。",
  retry_stop_pending: "仍在等待停止确认或结果对账，完成后再重试。",
  retry_delivery_closed: "PR 已关闭或暂停，请先重新打开并完成对账。",
  retry_result_pending: "结果仍待对账，请完成对账后再重试。",
  retry_flow_conflict: "同一 Issue 已有其他活跃 Flow，请先处理该 Flow。",
  retry_archived: "本轮已归档，请使用恢复推进开启新轮次。",
  retry_delivery_merged: "PR 已合并，不能重跑编码或评审任务。",
  retry_archive_inactive: "PR 已合并，只能重试活跃 Flow 的归档。",
  retry_archive_binding_missing: "原执行绑定已失效，暂时无法重试归档。",
  retry_execution_unsettled: "执行仍在运行或状态未确认，确认结束后再重试。",
  retry_archive_worker_unavailable:
    "原 Worker 尚未在线或仍有任务，暂时无法重试归档。",
  retry_archive_missing: "没有失败的归档任务可重试，请刷新确认状态。",
};

/** Conflicts are refusals, while a lost write response has an unknown outcome. Never auto-resend. */
export function roundFailureMessage(
  action: "archive" | "resume",
  error: unknown,
): string {
  const label = action === "archive" ? "结束本轮" : "恢复推进";
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后再操作。";
    if (error.status === 403) return "没有操作权限，或请求未通过同源校验。";
    if (error.status === 404) return "Flow 不存在，请刷新确认。";
    if (error.status === 409)
      return `未受理${label}：轮次、状态或安全条件不满足，请刷新核对后再操作（${error.message}）。`;
    if (error.status === 400 || error.status === 422)
      return `未受理${label}：请检查填写内容（${error.message}）。`;
  }
  return `无法确认${label}是否已提交，请先刷新核对状态，不要直接重复提交。`;
}

/** A lost response can follow a committed write; never advise blindly resending it. */
export function retryFailureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后再操作。";
    if (error.status === 403) return "没有重试权限，或请求未通过同源校验。";
    if (error.status === 404 || error.status === 409) {
      return (
        retryRefusals[error.message] ?? "暂时无法重试，请刷新确认当前状态。"
      );
    }
  }
  return "无法确认重试是否已提交，请先刷新核对状态，不要直接重复提交。";
}

export const getJobs = (
  id: number,
  cursor: string | null,
  signal?: AbortSignal,
) => api<JobPage>(`/api/flows/${id}/jobs`, { cursor, limit: 50 }, signal);
export const getInferenceNotices = (
  id: number,
  params: { cursor?: string | null; snapshot?: number },
  signal: AbortSignal,
) =>
  api<InferenceNoticePage>(
    `/api/flows/${id}/inference-notices`,
    { ...params, limit: 20 },
    signal,
  );
export const getActivity = (
  id: number,
  params: {
    cursor?: string | null;
    snapshot?: number;
    refresh?: boolean;
    since?: number;
    parent?: string;
  },
  signal?: AbortSignal,
) =>
  api<ActivityPage>(
    `/api/flows/${id}/activity`,
    { ...params, limit: 20 },
    signal,
  );
export const getContent = (
  id: number,
  key: string,
  version: number,
  offset: number,
  limit: number,
  signal?: AbortSignal,
) =>
  sessionRead(
    contentCache,
    [id, key, version, offset, limit],
    () =>
      api<ContentPage>(
        `/api/flows/${id}/activity/content`,
        { key, version, offset, limit },
        signal,
      ),
    signal,
  );
export const getDiff = (
  id: number,
  key: string,
  version: number,
  signal?: AbortSignal,
) =>
  sessionRead(
    contentCache,
    [id, key, version, "diff"],
    () =>
      api<ContentPage>(
        `/api/flows/${id}/activity/content`,
        { key, version, compare: true },
        signal,
      ),
    signal,
  );
export const getThread = (
  id: number,
  parent: string,
  cursor: string | null,
  snapshot: number,
  signal?: AbortSignal,
) =>
  sessionRead(
    threadCache,
    [id, parent, cursor, snapshot],
    () => getActivity(id, { parent, cursor, snapshot }, signal),
    signal,
  );
