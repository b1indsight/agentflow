import {
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { readActivity } from "./fixtures";
import type { Activity, Flow } from "./types";

export const ACTIVITY_CACHE_ROWS = 240;
export const CONVERSATION_CACHE_FLOWS = 4;
const PAGE_SIZE = 4;
interface State {
  items: Map<number, Activity>;
  count: number;
  hasMore: boolean;
  busy: boolean;
  newStart: number | null;
  checked: boolean;
  error: string | null;
  latestTime: string | null;
}
const initial = (): State => ({
  items: new Map(),
  count: 0,
  hasMore: true,
  busy: false,
  newStart: null,
  checked: false,
  error: null,
  latestTime: null,
});
const cache = new Map<number, State>();
function remember(id: number, state: State) {
  cache.delete(id);
  cache.set(id, state);
  for (const oldest of cache.keys()) {
    if (cache.size <= CONVERSATION_CACHE_FLOWS) break;
    cache.delete(oldest);
  }
}

/** The preview keeps sparse bounded pages, re-reading evicted rows at their original offsets. */
export function useConversation(
  flow: Flow,
  root: RefObject<HTMLElement | null>,
) {
  const [state, setState] = useState(() => cache.get(flow.id) ?? initial());
  const current = useRef(state);
  const request = useRef<AbortController | null>(null);
  const wanted = useRef<number[]>([]);
  const restoreFocus = useRef(false);
  const attempt = useRef({ offset: 0, refresh: false, frontier: true });
  const update = useCallback((next: State) => {
    current.current = next;
    setState(next);
  }, []);

  const read = useCallback(
    async (offset: number, refresh: boolean, frontier: boolean) => {
      const before = current.current;
      if (before.busy) return;
      const controller = new AbortController();
      request.current = controller;
      attempt.current = { offset, refresh, frontier };
      restoreFocus.current = Boolean(
        document.activeElement?.matches(".refresh-boundary"),
      );
      update({
        ...before,
        busy: true,
        error: null,
        newStart: refresh ? null : before.newStart,
      });
      try {
        const page = await readActivity(flow.id, {
          cursor: String(offset),
          refresh,
          signal: controller.signal,
        });
        if (controller.signal.aborted || request.current !== controller) return;
        const items = new Map(before.items);
        page.items.forEach((entry, index) => {
          items.delete(offset + index);
          items.set(offset + index, entry);
        });
        const protectedRows = new Set(wanted.current);
        for (const index of items.keys()) {
          if (items.size <= ACTIVITY_CACHE_ROWS) break;
          if (!protectedRows.has(index)) items.delete(index);
        }
        const next: State = {
          ...before,
          items,
          busy: false,
          error: null,
          count: Math.max(before.count, offset + page.items.length),
          hasMore: frontier ? page.hasMore : before.hasMore,
          newStart: refresh
            ? page.items.length
              ? before.count
              : null
            : before.newStart,
          checked: refresh && !page.items.length,
          latestTime: frontier
            ? (page.items.at(-1)?.time ?? before.latestTime)
            : before.latestTime,
        };
        remember(flow.id, next);
        update(next);
      } catch (error) {
        if (!controller.signal.aborted && request.current === controller) {
          console.error("Preview activity read failed", error);
          const next = {
            ...before,
            busy: false,
            error: "示例交互暂不可读取，已显示的记录仍然保留。",
          };
          remember(flow.id, next);
          update(next);
        }
      } finally {
        if (request.current === controller) request.current = null;
      }
    },
    [flow.id, update],
  );
  const advance = useCallback(
    (refresh: boolean) => {
      const before = current.current;
      if ((refresh && before.hasMore) || (!refresh && !before.hasMore)) return;
      void read(before.count, refresh, true);
    },
    [read],
  );
  const onRange = useCallback(
    (indexes: number[]) => {
      wanted.current = indexes;
      const before = current.current;
      if (before.busy || before.error) return;
      const missing = indexes.find((index) => !before.items.has(index));
      if (missing !== undefined)
        void read(Math.floor(missing / PAGE_SIZE) * PAGE_SIZE, false, false);
    },
    [read],
  );
  const retry = useCallback(() => {
    const { offset, refresh, frontier } = attempt.current;
    void read(offset, refresh, frontier);
  }, [read]);

  useEffect(() => {
    if (restoreFocus.current && !state.busy) {
      restoreFocus.current = false;
      if (document.activeElement === document.body) {
        (
          root.current?.querySelector<HTMLElement>(".refresh-boundary") ??
          root.current?.querySelector<HTMLElement>("#detail-panel")
        )?.focus({ preventScroll: true });
      }
    }
    if (state.busy || state.error) return;
    const missing = wanted.current.find((index) => !state.items.has(index));
    if (missing !== undefined) {
      void read(Math.floor(missing / PAGE_SIZE) * PAGE_SIZE, false, false);
      return;
    }
    if (!state.hasMore) return;
    const sentinel = root.current?.querySelector(".load-sentinel");
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) advance(false);
      },
      { root: root.current, rootMargin: "0px 0px 60px 0px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [advance, read, root, state]);
  useEffect(
    () => () => {
      request.current?.abort();
    },
    [],
  );
  return { state, advance, onRange, retry };
}
