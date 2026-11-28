import { useCallback, useEffect, useRef, useState } from "react";
import { getActivity } from "./api";
import { jsonCacheBytes, SessionCache } from "./sessionCache";
import type {
  ActivityIdentity,
  ActivityItem,
  ActivityPage,
  ActivityProvenancePage,
  ActivityProvenanceRange,
  ReadActivityProvenance,
  SourceStatus,
} from "./types";

export const ACTIVITY_CACHE_ROWS = 240;
export const CONVERSATION_CACHE_FLOWS = 4;
export const ACTIVITY_PROVENANCE_ROWS = 20;
type Page = {
  cursor: string | null;
  snapshot: number;
  records: ActivityIdentity[];
  upper: ActivityIdentity;
};
type Read = "next" | "refresh" | "revisit" | "body";

/** Pages retain their own version until revisited; only full items are evictable. */
export interface ConversationState {
  items: Map<string, ActivityItem>;
  records: ActivityIdentity[];
  pages: Page[];
  snapshot: number | null;
  total: number;
  next_cursor: string | null;
  has_more: boolean;
  sources: SourceStatus[];
  changes: Record<string, number>;
  busy: boolean;
  error: { message: string; cursor: string | null; mode: Read } | null;
}
const empty = (): ConversationState => ({
  items: new Map(),
  records: [],
  pages: [],
  snapshot: null,
  total: 0,
  next_cursor: null,
  has_more: true,
  sources: [],
  changes: {},
  busy: false,
  error: null,
});
const cache = new SessionCache<number, ConversationState>(
  CONVERSATION_CACHE_FLOWS,
  16 * 1024 * 1024,
  (id, state) =>
    jsonCacheBytes(id, {
      ...state,
      records: undefined,
      items: [...state.items],
    }),
);

/** Project only the fields needed for grouping, provenance boundaries and logical navigation. */
export function activityIdentity(entry: ActivityItem): ActivityIdentity {
  const metadata: ActivityIdentity["metadata"] = {};
  for (const key of [
    "attempt_id",
    "session_id",
    "session_generation",
    "call_id",
  ] as const)
    if (Object.hasOwn(entry.metadata, key)) metadata[key] = entry.metadata[key];
  return {
    key: entry.key,
    version: entry.version,
    attempt_id: entry.attempt_id,
    source: entry.source,
    kind: entry.kind,
    author: entry.author,
    occurred_at: entry.occurred_at,
    body_length: entry.body_length,
    parent_key: entry.parent_key,
    prior: entry.prior,
    deleted: entry.deleted,
    metadata,
  };
}

// Match PostgreSQL's timestamptz precision and bytewise UTF-8 tie breaker.
function comparePosition(a: ActivityIdentity, b: ActivityIdentity) {
  const millis = Date.parse(a.occurred_at) - Date.parse(b.occurred_at);
  if (millis) return millis;
  const micros = (time: string) =>
    Number((time.match(/\.(\d+)/)?.[1] ?? "").padEnd(6, "0").slice(3, 6));
  const fraction = micros(a.occurred_at) - micros(b.occurred_at);
  if (fraction || a.key === b.key) return fraction;
  const left = Array.from(a.key),
    right = Array.from(b.key);
  for (let i = 0; i < Math.min(left.length, right.length); i++) {
    const diff = left[i].codePointAt(0)! - right[i].codePointAt(0)!;
    if (diff) return diff;
  }
  return left.length - right.length;
}

/** Append a live tail, or replace only the revisited interval in the new snapshot. */
export function mergeActivityPage(
  before: ConversationState,
  page: ActivityPage,
  cursor: string | null,
  wanted: ReadonlySet<string>,
  range?: Page,
): ConversationState {
  if (range && page.snapshot !== before.snapshot)
    throw new Error("交互快照不一致，请刷新后重试");
  if (page.has_more && (!page.next_cursor || page.next_cursor === cursor))
    throw new Error("交互分页未前进，请重试");
  if (!range && !page.items.length)
    return {
      ...before,
      snapshot: page.snapshot,
      total: page.total,
      next_cursor: page.next_cursor,
      has_more: page.has_more,
      sources: page.sources,
      changes: page.changes,
      busy: false,
      error: null,
    };
  const accepted = range
    ? page.items.filter((item) => comparePosition(item, range.upper) <= 0)
    : page.items;
  const identities = accepted.map(activityIdentity);
  const keys = new Set(identities.map((item) => item.key));
  const last = identities.at(-1);
  const split =
    range &&
    last &&
    page.has_more &&
    comparePosition(page.items.at(-1)!, range.upper) < 0;
  const replacement: Page[] = [
    {
      cursor,
      snapshot: page.snapshot,
      records: identities,
      upper: range && !split ? range.upper : last!,
    },
  ];
  if (split && last) {
    const remaining = range.records.filter(
      (item) => comparePosition(item, last) > 0 && !keys.has(item.key),
    );
    replacement.push({
      ...range,
      cursor: page.next_cursor,
      records: remaining,
    });
  }
  // A corrected occurrence time may move an old identity into the returned interval.
  const pages = before.pages.flatMap((old) => {
    if (old === range) return replacement;
    if (
      old.snapshot === page.snapshot ||
      !old.records.some((item) => keys.has(item.key))
    )
      return [old];
    const records = old.records.filter((item) => !keys.has(item.key));
    return [{ ...old, records }];
  });
  if (!range) pages.push(...replacement);
  const items = new Map(before.items);
  if (range)
    for (const old of range.records) {
      if (!split || comparePosition(old, last!) <= 0) items.delete(old.key);
    }
  return {
    ...before,
    pages,
    records: pages.flatMap((p) => p.records),
    items: cacheBodies(items, accepted, wanted),
    snapshot: page.snapshot,
    total: page.total,
    next_cursor: range ? before.next_cursor : page.next_cursor,
    has_more: range ? before.has_more : page.has_more,
    sources: page.sources,
    changes: range ? before.changes : page.changes,
    busy: false,
    error: null,
  };
}

function cacheBodies(
  items: Map<string, ActivityItem>,
  loaded: ActivityItem[],
  wanted: ReadonlySet<string>,
) {
  for (const item of loaded) items.set(item.key, item);
  for (const key of items.keys()) {
    if (items.size <= ACTIVITY_CACHE_ROWS) break;
    if (!wanted.has(key)) items.delete(key);
  }
  return items;
}

async function pageBodies(flowId: number, range: Page, signal: AbortSignal) {
  const page = await getActivity(
    flowId,
    { cursor: range.cursor, snapshot: range.snapshot },
    signal,
  );
  signal.throwIfAborted();
  return range.records.map((record) => {
    const item = page.items.find(
      (i) => i.key === record.key && i.version === record.version,
    );
    if (!item || page.snapshot !== range.snapshot)
      throw new Error("交互快照不一致，请刷新后重试");
    return item;
  });
}

/** Read one metadata-only source page; archive reads neither pin nor populate the full-body cache. */
export async function readConversationProvenance(
  flowId: number,
  state: ConversationState,
  range: ActivityProvenanceRange,
  offset: number,
  signal: AbortSignal,
): Promise<ActivityProvenancePage> {
  const total = range.last - range.first + 1;
  if (
    state.snapshot === null ||
    !Number.isInteger(range.first) ||
    !Number.isInteger(range.last) ||
    range.first < 0 ||
    range.last >= state.records.length ||
    !Number.isInteger(offset) ||
    offset < 0 ||
    offset >= total
  )
    throw new Error("来源记录范围无效，请重新打开来源信息");
  const end = Math.min(
    range.first + offset + ACTIVITY_PROVENANCE_ROWS,
    range.last + 1,
  );
  const items: ActivityProvenancePage["items"] = [];
  let fetchedPosition: Page | undefined;
  let fetchedItems: ActivityItem[] = [];
  for (let index = range.first + offset; index < end; index++) {
    signal.throwIfAborted();
    const record = state.records[index];
    let item = state.items.get(record.key);
    if (!item) {
      const position = state.pages.find((page) =>
        page.records.includes(record),
      );
      if (!position) throw new Error("来源记录游标缺失，请刷新后重试");
      if (position !== fetchedPosition) {
        fetchedItems = await pageBodies(flowId, position, signal);
        fetchedPosition = position;
      }
      item = fetchedItems[position.records.indexOf(record)];
    }
    const {
      key,
      version,
      attempt_id,
      source,
      author,
      occurred_at,
      url,
      metadata,
    } = item;
    items.push({
      key,
      version,
      attempt_id,
      source,
      author,
      occurred_at,
      url,
      metadata,
    });
  }
  return {
    items,
    offset,
    total,
    next_offset: offset + items.length < total ? offset + items.length : null,
  };
}

/**
 * Keep read identities and opaque cursors after body eviction. WindowList owns all pixel restoration;
 * Mounted keys demand bodies; only keys the user scrolls back to demand a newer page.
 */
export function useConversation(
  flowId: number,
  refresh: number | null,
  lastRefresh: React.RefObject<number>,
) {
  const [state, setState] = useState(() => cache.get(flowId) ?? empty());
  const historyOnly = refresh === null;
  const current = useRef(state),
    controller = useRef<AbortController | null>(null),
    wanted = useRef(new Set<string>()),
    revisit = useRef(new Set<string>());
  const put = (s: ConversationState) => {
    current.current = s;
    // Keep the completed snapshot during a pending read, without weighing its unchanged payload again.
    if (!s.busy) cache.set(flowId, s);
    setState(s);
  };
  const read = useCallback(
    async (mode: Read, range?: Page) => {
      controller.current?.abort();
      const c = new AbortController();
      controller.current = c;
      const before = current.current;
      const cursor = range ? range.cursor : before.next_cursor;
      if (!range) revisit.current.clear();
      put({ ...before, busy: true, error: null });
      try {
        if (mode === "body" && range) {
          const loaded = await pageBodies(flowId, range, c.signal);
          if (!c.signal.aborted)
            put({
              ...before,
              busy: false,
              error: null,
              items: cacheBodies(new Map(before.items), loaded, wanted.current),
            });
          return;
        }
        const page = await getActivity(
          flowId,
          {
            cursor,
            snapshot:
              range || historyOnly ? (before.snapshot ?? undefined) : undefined,
            ...(mode === "refresh" && {
              refresh: true,
              since: before.snapshot ?? undefined,
            }),
          },
          c.signal,
        );
        if (!c.signal.aborted)
          put(mergeActivityPage(before, page, cursor, wanted.current, range));
      } catch (e) {
        if (!c.signal.aborted)
          put({
            ...before,
            busy: false,
            error: {
              message: e instanceof Error ? e.message : "读取交互失败",
              cursor,
              mode,
            },
          });
      }
    },
    [flowId, historyOnly],
  );
  const load = useCallback(
    (refresh = false) => {
      const s = current.current;
      if (s.busy || (!refresh && !s.has_more)) return false;
      void read(refresh ? "refresh" : "next");
      return true;
    },
    [read],
  );
  const fillViewport = useCallback(() => {
    const s = current.current;
    if (s.busy || s.error) return;
    // A correction can move every old identity out of an interval. Keep its bounds and
    // revisit it with the nearest visible page rather than losing backfills in that gap.
    const visible = (p: Page | undefined) =>
      p?.records.some((i) => revisit.current.has(i.key));
    const stale =
      revisit.current.size &&
      s.pages.find((p, index) => {
        if (p.snapshot === s.snapshot) return false;
        if (p.records.length) return visible(p);
        let left = index - 1,
          right = index + 1;
        while (left >= 0 && !s.pages[left].records.length) left--;
        while (right < s.pages.length && !s.pages[right].records.length)
          right++;
        return visible(s.pages[left]) || visible(s.pages[right]);
      });
    if (stale) {
      void read("revisit", stale);
      return;
    }
    for (const key of wanted.current) {
      if (s.items.has(key)) continue;
      const page = s.pages.find((p) => p.records.some((i) => i.key === key));
      if (page) {
        void read("body", page);
        return;
      }
    }
  }, [read]);
  const onRange = useCallback(
    (keys: string[]) => {
      wanted.current = new Set(keys);
      fillViewport();
    },
    [fillViewport],
  );
  const onLookBack = useCallback(
    (keys: readonly string[]) => {
      revisit.current = new Set(keys);
      fillViewport();
    },
    [fillViewport],
  );
  const snapshot = state.snapshot;
  const readProvenance = useCallback<ReadActivityProvenance>(
    (range, offset, signal) => {
      const s = current.current;
      if (s.snapshot !== snapshot)
        return Promise.reject(new Error("来源快照已更新，请重新打开来源信息"));
      return readConversationProvenance(flowId, s, range, offset, signal);
    },
    [flowId, snapshot],
  );
  useEffect(fillViewport, [state, fillViewport]);
  useEffect(() => {
    // The detail owns this watermark: a send on another tab must refresh even a cached snapshot.
    // Don't consume it when a tab switch or StrictMode aborts the in-flight read.
    let mounted = true;
    if (refresh !== null && lastRefresh.current !== refresh) {
      void read("refresh").then(() => {
        if (mounted) lastRefresh.current = refresh;
      });
    } else if (current.current.snapshot === null) void read("next");
    return () => {
      mounted = false;
      controller.current?.abort();
    };
  }, [read, refresh, lastRefresh]);
  return {
    state,
    load,
    onRange,
    onLookBack,
    readProvenance,
    retry: () => {
      const { error, busy } = current.current;
      if (error && !busy)
        void read(
          error.mode,
          error.mode === "body" || error.mode === "revisit"
            ? current.current.pages.find((p) => p.cursor === error.cursor)
            : undefined,
        );
    },
  };
}
