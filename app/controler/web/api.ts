import type {
  ActivityPage,
  ContentPage,
  FlowDetail,
  FlowPage,
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
      if (body.detail) code = body.detail;
    } catch {
      // A proxy may return an empty error body; status remains actionable.
    }
    throw new ApiError(code, response.status);
  }
  return response.json() as Promise<T>;
}

export const getFlows = (
  params: Record<string, string | number | undefined>,
  signal?: AbortSignal,
) => api<FlowPage>("/api/flows", params, signal);
export const getRepos = (cursor: string | null, signal?: AbortSignal) =>
  api<RepositoryPage>("/api/repos", { cursor, limit: 100 }, signal);
export const getFlow = (id: number, signal?: AbortSignal) =>
  api<FlowDetail>(`/api/flows/${id}`, {}, signal);
export const archiveFlow = (id: number) =>
  api<FlowDetail>(`/api/flows/${id}/archive`, {}, undefined, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
export const getJobs = (
  id: number,
  cursor: string | null,
  signal?: AbortSignal,
) => api<JobPage>(`/api/flows/${id}/jobs`, { cursor, limit: 50 }, signal);
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
  api<ContentPage>(
    `/api/flows/${id}/activity/content`,
    { key, version, offset, limit },
    signal,
  );
export const getDiff = (
  id: number,
  key: string,
  version: number,
  signal?: AbortSignal,
) =>
  api<ContentPage>(
    `/api/flows/${id}/activity/content`,
    { key, version, compare: true },
    signal,
  );
export const getThread = (
  id: number,
  parent: string,
  cursor: string | null,
  snapshot: number,
  signal?: AbortSignal,
) => getActivity(id, { parent, cursor, snapshot }, signal);
