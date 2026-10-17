import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { getActivity } from "./api";
import type { ActivityItem, SourceStatus } from "./types";

export const ACTIVITY_CACHE_ROWS = 240;
export const CONVERSATION_CACHE_FLOWS = 4;
type Page = { cursor: string | null; indexes: number[] };
type State = {
  items: Map<number, ActivityItem>;
  keys: string[];
  pages: Page[];
  snapshot: number | null;
  total: number;
  next_cursor: string | null;
  has_more: boolean;
  sources: SourceStatus[];
  changes: Record<string, number>;
  busy: boolean;
  error: { message: string; cursor: string | null; refresh: boolean } | null;
};
const empty = (): State => ({
  items: new Map(),
  keys: [],
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
const cache = new Map<number, State>();
function remember(id: number, s: State) {
  cache.delete(id);
  cache.set(id, { ...s, busy: false });
  while (cache.size > CONVERSATION_CACHE_FLOWS)
    cache.delete(cache.keys().next().value!);
}
/** Keeps opaque page cursors after body eviction so virtual windows can be re-read in one pinned snapshot. */
export function useConversation(
  flowId: number,
  root: RefObject<HTMLElement | null>,
) {
  const [state, setState] = useState(() => cache.get(flowId) ?? empty());
  const current = useRef(state),
    controller = useRef<AbortController | null>(null),
    wanted = useRef<number[]>([]);
  const put = (s: State) => {
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
      const viewport = root.current;
      const anchor = refresh
        ? [
            ...(viewport?.querySelectorAll<HTMLElement>("[data-entry]") ?? []),
          ].find(
            (x) =>
              x.getBoundingClientRect().bottom >
              (viewport?.getBoundingClientRect().top ?? 0),
          )
        : undefined;
      const anchorKey = anchor?.dataset.entry;
      const anchorTop = anchor?.getBoundingClientRect().top ?? 0;
      const scrollTop = viewport?.scrollTop ?? 0;
      put({ ...before, busy: true, error: null });
      try {
        const p = await getActivity(
          flowId,
          refresh
            ? { refresh: true, since: before.snapshot ?? undefined }
            : { cursor, snapshot: before.snapshot ?? undefined },
          c.signal,
        );
        if (c.signal.aborted) return;
        if (refresh) {
          if (p.snapshot === before.snapshot) {
            put({
              ...before,
              sources: p.sources,
              changes: p.changes,
              busy: false,
              error: null,
            });
            return;
          }
          // Rebuild cursor identities until the reading anchor, keeping at most 240 bodies.
          // Old and new snapshots are never merged, even when an early edit/backfill moves rows.
          const keys = p.items.map((x) => x.key),
            items = new Map(p.items.map((x, i) => [i, x]));
          const pages: Page[] = [
            { cursor: null, indexes: p.items.map((_, i) => i) },
          ];
          let last = p;
          while (anchorKey && !keys.includes(anchorKey) && last.has_more) {
            const cursor = last.next_cursor;
            last = await getActivity(
              flowId,
              { cursor, snapshot: p.snapshot },
              c.signal,
            );
            if (c.signal.aborted) return;
            const start = keys.length;
            last.items.forEach((x, i) => {
              keys.push(x.key);
              items.set(start + i, x);
            });
            pages.push({
              cursor,
              indexes: last.items.map((_, i) => start + i),
            });
            while (items.size > ACTIVITY_CACHE_ROWS)
              items.delete(items.keys().next().value!);
          }
          put({
            items,
            keys,
            pages,
            snapshot: p.snapshot,
            total: p.total,
            next_cursor: last.next_cursor,
            has_more: last.has_more,
            sources: p.sources,
            changes: p.changes,
            busy: false,
            error: null,
          });
          requestAnimationFrame(() => {
            if (!viewport || c.signal.aborted || !anchorKey) return;
            viewport.scrollTop =
              scrollTop +
              (keys.indexOf(anchorKey) - before.keys.indexOf(anchorKey)) * 220;
            requestAnimationFrame(() => {
              const restored = [
                ...viewport.querySelectorAll<HTMLElement>("[data-entry]"),
              ].find((x) => x.dataset.entry === anchorKey);
              if (restored)
                viewport.scrollTop +=
                  restored.getBoundingClientRect().top - anchorTop;
            });
          });
          return;
        }
        const existing = before.pages.find((page) => page.cursor === cursor),
          start = existing?.indexes[0] ?? before.keys.length,
          keys = existing
            ? before.keys
            : [...before.keys, ...p.items.map((x) => x.key)],
          items = new Map(before.items);
        p.items.forEach((x, i) => items.set(start + i, x));
        while (items.size > ACTIVITY_CACHE_ROWS) {
          const removable = [...items.keys()].find(
            (i) => !wanted.current.includes(i),
          );
          if (removable === undefined) break;
          items.delete(removable);
        }
        put({
          ...before,
          items,
          keys,
          pages: existing
            ? before.pages
            : [
                ...before.pages,
                { cursor, indexes: p.items.map((_, i) => start + i) },
              ],
          snapshot: p.snapshot,
          total: p.total,
          next_cursor: existing ? before.next_cursor : p.next_cursor,
          has_more: existing ? before.has_more : p.has_more,
          sources: p.sources,
          changes: p.changes,
          busy: false,
          error: null,
        });
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
    [flowId, root],
  );
  const load = useCallback(
    (refresh = false) => {
      const s = current.current;
      if (s.busy || (!refresh && !s.has_more)) return;
      void read(refresh ? null : s.next_cursor, refresh);
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
  useEffect(() => {
    if (!state.busy && !state.error && wanted.current.length)
      onRange(wanted.current);
  }, [state.busy, state.error, state.items, onRange]);
  useEffect(() => {
    // StrictMode deliberately aborts the first mount; do not retain its in-flight flag.
    if (!current.current.keys.length) void read(null);
    return () => controller.current?.abort();
  }, [flowId, read]);
  useEffect(() => {
    if (state.busy || state.error || !state.has_more) return;
    const sentinel = root.current?.querySelector(".load-sentinel");
    if (!sentinel) return;
    const o = new IntersectionObserver(
      (x) => x.some((e) => e.isIntersecting) && load(),
      { root: root.current, rootMargin: "0px 0px 120px 0px" },
    );
    o.observe(sentinel);
    return () => o.disconnect();
  }, [load, root, state.busy, state.error, state.has_more]);
  return {
    state,
    load,
    onRange,
    retry: () => {
      const { error, busy } = current.current;
      if (error && !busy) void read(error.cursor, error.refresh);
    },
  };
}
