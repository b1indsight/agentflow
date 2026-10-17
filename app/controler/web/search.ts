import type { Flow, Phase } from "./types";
import { phases } from "./phases";

export interface SearchQuery {
  terms: string[];
  repo?: string;
  lifecycle?: "active" | "done";
  status?: Phase;
  issue?: number;
  pr?: number;
}

/** Parse the preview's documented subset; malformed qualifiers must never silently broaden results. */
export function parseSearch(
  raw: string,
  repos: readonly string[],
): { ok: true; query: SearchQuery } | { ok: false; error: string } {
  const tokens = raw.match(/(?:[^\s"]|"[^"]*")+/g) ?? [];
  if ((raw.match(/"/g)?.length ?? 0) % 2)
    return { ok: false, error: "请补全成对的双引号。" };
  const query: SearchQuery = { terms: [] };
  const seen = new Set<string>();
  for (const token of tokens) {
    if (token.startsWith('"') || !token.includes(":")) {
      query.terms.push(token.replaceAll('"', "").toLowerCase());
      continue;
    }
    const colon = token.indexOf(":"),
      key = token.slice(0, colon).toLowerCase(),
      value = token.slice(colon + 1).replaceAll('"', "");
    if (seen.has(key))
      return { ok: false, error: `筛选条件 ${key}: 只能出现一次。` };
    seen.add(key);
    switch (key) {
      case "repo": {
        const repo = value.replace(/^b1indsight\//, "");
        if (!repos.includes(repo))
          return { ok: false, error: "请选择已有仓库。" };
        query.repo = repo;
        break;
      }
      case "is":
        if (value !== "active" && value !== "done")
          return {
            ok: false,
            error: "is: 支持 active（活跃）或 done（已完成）。",
          };
        query.lifecycle = value;
        break;
      case "status": {
        const phase = phases.find(([name]) => name === value)?.[0];
        if (!phase)
          return {
            ok: false,
            error: "请选择排队、执行、审核、受阻或完成状态。",
          };
        query.status = phase;
        break;
      }
      case "issue":
      case "pr": {
        const number = Number(value);
        if (
          !/^\d+$/.test(value) ||
          !Number.isSafeInteger(number) ||
          number <= 0
        )
          return { ok: false, error: `${key}: 后请输入有效的正整数编号。` };
        query[key] = number;
        break;
      }
      default:
        return {
          ok: false,
          error: `暂不支持 ${key}:，可用 repo:、is:、status:、issue:、pr:。`,
        };
    }
  }
  return { ok: true, query };
}

export function matchesFlow(flow: Flow, query: SearchQuery) {
  const text =
    `${flow.title} ${flow.id} ${flow.issue} ${flow.pr ?? ""} ${flow.repo}`.toLowerCase();
  return (
    (!query.repo || flow.repo === query.repo) &&
    (!query.lifecycle ||
      (query.lifecycle === "done"
        ? flow.phase === "done"
        : flow.phase !== "done")) &&
    (!query.status || flow.phase === query.status) &&
    (!query.issue || flow.issue === query.issue) &&
    (!query.pr || flow.pr === query.pr) &&
    query.terms.every((term) => text.includes(term))
  );
}
