import { useCallback, useEffect, useRef, useState } from "react";
import { getActivity } from "./api";
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
type Page = { cursor: string | null; indexes: number[] };

/** All read identities and cursors belong to one snapshot; only full items are evictable. */
export interface ConversationState {
  items: Map<number, ActivityItem>;
  records: ActivityIdentity[];
  pages: Page[];
  snapshot: number | null;
  total: number;
  next_cursor: string | null;
  has_more: boolean;
  sources: SourceStatus[];
  changes: Record<string, number>;
  busy: boolean;
  error: { message: string; cursor: string | null; refresh: boolean } | null;
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
const cache = new Map<number, ConversationState>();
function remember(id: number, s: ConversationState) {
  cache.delete(id);
  cache.set(id, { ...s, busy: false });
  while (cache.size > CONVERSATION_CACHE_FLOWS)
    cache.delete(cache.keys().next().value!);
}

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

function checkPage(
  state: ConversationState,
  page: ActivityPage,
  position?: Page,
) {
  if (
    (state.snapshot !== null && page.snapshot !== state.snapshot) ||
    (position &&
      (page.items.length !== position.indexes.length ||
        page.items.some((item, index) => {
          const identity = state.records[position.indexes[index]];
          return (
            identity?.key !== item.key || identity.version !== item.version
          );
        })))
  )
    throw new Error("交互快照不一致，请刷新后重试");
}

function appendPage(
  before: ConversationState,
  page: ActivityPage,
  cursor: string | null,
  wanted: readonly number[],
): ConversationState {
  const existing = before.pages.find((position) => position.cursor === cursor);
  checkPage(before, page, existing);
  if (page.has_more && (!page.next_cursor || page.next_cursor === cursor))
    throw new Error("交互分页未前进，请重试");
  const start = existing?.indexes[0] ?? before.records.length;
  const items = new Map(before.items);
  page.items.forEach((item, index) => items.set(start + index, item));
  const protectedIndexes = new Set(wanted);
  for (const index of items.keys()) {
    if (items.size <= ACTIVITY_CACHE_ROWS) break;
    if (!protectedIndexes.has(index)) items.delete(index);
  }
  // Even a malformed caller range must not turn the full-body cache into an unbounded store.
  while (items.size > ACTIVITY_CACHE_ROWS)
    items.delete(items.keys().next().value!);
  return {
    ...before,
    items,
    records: existing
      ? before.records
      : [...before.records, ...page.items.map(activityIdentity)],
    pages: existing
      ? before.pages
      : [
          ...before.pages,
          { cursor, indexes: page.items.map((_, index) => start + index) },
        ],
    snapshot: page.snapshot,
    total: page.total,
    next_cursor: existing ? before.next_cursor : page.next_cursor,
    has_more: existing ? before.has_more : page.has_more,
    sources: page.sources,
    changes: page.changes,
    busy: false,
    error: null,
  };
}

/**
 * Rebuild one pinned snapshot up to the latest logical reading target, never a request-time DOM row.
 * A missing target falls back through known identities once its old ordered position is passed.
 */
export async function refreshConversationSnapshot(
  flowId: number,
  before: ConversationState,
  readAnchor: () => string | null,
  signal: AbortSignal,
): Promise<{ state: ConversationState; anchorKey: string | null }> {
  const first = await getActivity(
    flowId,
    { refresh: true, since: before.snapshot ?? undefined },
    signal,
  );
  signal.throwIfAborted();
  if (first.snapshot === before.snapshot)
    return {
      state: {
        ...before,
        sources: first.sources,
        changes: first.changes,
        busy: false,
        error: null,
      },
      anchorKey: readAnchor(),
    };

  const previousIndexes = new Map(
    before.records.map((identity, index) => [identity.key, index]),
  );
  const present = new Set<string>();
  let furthestPrevious = -1;
  let state = empty();
  let page = first;
  let cursor: string | null = null;
  for (;;) {
    state = appendPage(state, page, cursor, []);
    for (const item of page.items) {
      present.add(item.key);
      furthestPrevious = Math.max(
        furthestPrevious,
        previousIndexes.get(item.key) ?? -1,
      );
    }
    // Read again after every awaited page: the user may have continued scrolling while it loaded.
    const requested = readAnchor();
    let anchorKey = requested;
    const previousIndex =
      requested === null ? undefined : previousIndexes.get(requested);
    if (
      requested !== null &&
      !present.has(requested) &&
      previousIndex !== undefined
    ) {
      const target = before.records[previousIndex];
      const frontier = state.records.at(-1);
      // The API orders by (occurred_at, key). Do not chase a removed last-known key into unread history.
      const passedPosition =
        frontier &&
        (Date.parse(frontier.occurred_at) > Date.parse(target.occurred_at) ||
          (frontier.occurred_at === target.occurred_at &&
            frontier.key > target.key));
      if (
        state.has_more &&
        furthestPrevious <= previousIndex &&
        !passedPosition
      ) {
        cursor = state.next_cursor;
        page = await getActivity(
          flowId,
          { cursor, snapshot: first.snapshot },
          signal,
        );
        signal.throwIfAborted();
        continue;
      }
      anchorKey = null;
      for (let index = previousIndex - 1; index >= 0; index--)
        if (present.has(before.records[index].key)) {
          anchorKey = before.records[index].key;
          break;
        }
      if (anchorKey === null)
        for (
          let index = previousIndex + 1;
          index < before.records.length;
          index++
        )
          if (present.has(before.records[index].key)) {
            anchorKey = before.records[index].key;
            break;
          }
    } else if (requested !== null && !present.has(requested)) anchorKey = null;
    return {
      state: { ...state, sources: first.sources, changes: first.changes },
      anchorKey,
    };
  }
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
    let item = state.items.get(index);
    if (!item) {
      const position = state.pages.find((page) => page.indexes.includes(index));
      if (!position) throw new Error("来源记录游标缺失，请刷新后重试");
      if (position !== fetchedPosition) {
        const page = await getActivity(
          flowId,
          { cursor: position.cursor, snapshot: state.snapshot },
          signal,
        );
        signal.throwIfAborted();
        checkPage(state, page, position);
        fetchedPosition = position;
        fetchedItems = page.items;
      }
      item = fetchedItems[position.indexes.indexOf(index)];
    }
    const { key, version, source, author, occurred_at, url, metadata } = item;
    items.push({ key, version, source, author, occurred_at, url, metadata });
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
 * callers report raw mounted indexes with onRange and the latest raw reading key with onAnchor.
 */
export function useConversation(flowId: number) {
  const [state, setState] = useState(() => cache.get(flowId) ?? empty());
  const current = useRef(state),
    controller = useRef<AbortController | null>(null),
    wanted = useRef<number[]>([]),
    anchor = useRef<string | null>(null);
  const put = (s: ConversationState) => {
    current.current = s;
    remember(flowId, s);
    setState(s);
  };
  const read = useCallback(
    async (cursor: string | null, refresh = false) => {
      controller.current?.abort();
      const c = new AbortController();
      controller.current = c;
      const before = current.current;
      put({ ...before, busy: true, error: null });
      try {
        if (refresh) {
          const result = await refreshConversationSnapshot(
            flowId,
            before,
            () => anchor.current,
            c.signal,
          );
          if (c.signal.aborted) return;
          const indexes = new Map(
            result.state.records.map((item, index) => [item.key, index]),
          );
          wanted.current = wanted.current.flatMap((index) => {
            const next = indexes.get(before.records[index]?.key);
            return next === undefined ? [] : [next];
          });
          anchor.current = result.anchorKey;
          put(result.state);
          return;
        }
        const page = await getActivity(
          flowId,
          { cursor, snapshot: before.snapshot ?? undefined },
          c.signal,
        );
        if (!c.signal.aborted)
          put(appendPage(before, page, cursor, wanted.current));
      } catch (e) {
        if (!c.signal.aborted)
          put({
            ...before,
            busy: false,
            error: {
              message: e instanceof Error ? e.message : "读取交互失败",
              cursor,
              refresh,
            },
          });
      }
    },
    [flowId],
  );
  const load = useCallback(
    (refresh = false) => {
      const s = current.current;
      if (s.busy || (!refresh && !s.has_more)) return false;
      void read(refresh ? null : s.next_cursor, refresh);
      return true;
    },
    [read],
  );
  const onRange = useCallback(
    (indexes: number[]) => {
      wanted.current = indexes;
      const missing = indexes.find((i) => !current.current.items.has(i));
      if (
        missing !== undefined &&
        !current.current.busy &&
        !current.current.error
      ) {
        const page = current.current.pages.find((p) =>
          p.indexes.includes(missing),
        );
        if (page) void read(page.cursor);
      }
    },
    [read],
  );
  const onAnchor = useCallback((key: string | null) => {
    anchor.current = key;
  }, []);
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
  useEffect(() => {
    if (!state.busy && !state.error && wanted.current.length)
      onRange(wanted.current);
  }, [state.busy, state.error, state.items, onRange]);
  useEffect(() => {
    // StrictMode deliberately aborts the first mount; do not retain its in-flight flag.
    if (!current.current.records.length) void read(null);
    return () => controller.current?.abort();
  }, [flowId, read]);
  return {
    state,
    load,
    onRange,
    onAnchor,
    readProvenance,
    retry: () => {
      const { error, busy } = current.current;
      if (error && !busy) void read(error.cursor, error.refresh);
    },
  };
}
