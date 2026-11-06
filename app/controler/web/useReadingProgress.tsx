import {
  createContext,
  type ReactNode,
  type RefObject,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { ApiError, getReading, getReadingResume, writeReading } from "./api";
import { mergeReadRanges, visibleRegion } from "./readingVisibility";
import type {
  ActivityItem,
  EntryReading,
  ReadObservation,
  ReadingAnchor,
  ReadingProgress,
  ReadingResume,
} from "./types";

const fresh = (): EntryReading => ({
  generation: 0,
  header_read: false,
  ranges: [],
  explicit_read: false,
  unread: true,
});
const identity = (entry: ActivityItem) =>
  JSON.stringify([entry.key, entry.read_revision]);
type Entry = Pick<
  ActivityItem,
  | "key"
  | "version"
  | "read_revision"
  | "parent_key"
  | "occurred_at"
  | "body_length"
  | "reading"
> & { snapshot: number };
type ReadingContextValue = {
  anchor: ReadingAnchor | null;
  snapshot: number;
  register: (entry: ActivityItem, snapshot: number) => void;
  local: Map<string, EntryReading>;
  markEntry: (entry: ActivityItem, read: boolean) => Promise<void>;
};
const ReadingContext = createContext<ReadingContextValue | null>(null);

/** Collect exact visible ranges, retain failed batches in this page, and fence position writers. */
export function useReadingProgress(
  flowId: number,
  root: RefObject<HTMLElement | null>,
  snapshot: number | null,
  restore: (resume: ReadingResume) => void,
) {
  const [state, setState] = useState<ReadingProgress | null>(null);
  const stateRef = useRef(state);
  const [error, setError] = useState("");
  const [positionConflict, setPositionConflict] = useState(false);
  const [resume, setResume] = useState<ReadingResume | null>(null);
  const [preview, setPreview] = useState<ReadingProgress | null>(null);
  const previewing = useRef(false);
  const [local, setLocal] = useState(new Map<string, EntryReading>());
  const localRef = useRef(local);
  const registry = useRef(new Map<string, Entry>());
  const queue = useRef(new Map<string, ReadObservation>());
  const suppressed = useRef(new Set<string>());
  const emitted = useRef(new Set<string>());
  const session = useRef(crypto.randomUUID());
  const writer = useRef<{ epoch: number; sequence: number } | null>(null);
  const blocked = useRef(false),
    ready = useRef(false),
    sending = useRef(false),
    positioning = useRef(false);
  const pendingPosition = useRef(false);
  const active = useRef<AbortController | null>(null);
  const retryBlocked = useRef(false);
  const restoreRef = useRef(restore);
  restoreRef.current = restore;

  const accept = useCallback((next: ReadingProgress) => {
    const previous = stateRef.current;
    const receiptsNewer =
      !previous ||
      (next.receipts_version >= previous.receipts_version &&
        next.snapshot >= previous.snapshot);
    const positionNewer =
      !previous || next.position_revision >= previous.position_revision;
    const merged = {
      ...next,
      ...(!receiptsNewer && previous
        ? {
            receipts_version: previous.receipts_version,
            snapshot: previous.snapshot,
            unread_count: previous.unread_count,
          }
        : {}),
      ...(!positionNewer && previous
        ? {
            position_revision: previous.position_revision,
            writer_epoch: previous.writer_epoch,
            anchor: previous.anchor,
            saved_at: previous.saved_at,
          }
        : {}),
    };
    stateRef.current = merged;
    setState(merged);
    return receiptsNewer;
  }, []);

  const updateLocal = useCallback((id: string, value: EntryReading) => {
    const next = new Map(localRef.current).set(id, value);
    localRef.current = next;
    setLocal(next);
  }, []);

  const register = useCallback(
    (entry: ActivityItem, currentSnapshot: number) => {
      if (!entry.read_revision) return;
      registry.current.set(identity(entry), {
        key: entry.key,
        version: entry.version,
        read_revision: entry.read_revision,
        parent_key: entry.parent_key,
        occurred_at: entry.occurred_at,
        body_length: entry.body_length,
        reading: entry.reading,
        snapshot: currentSnapshot,
      });
      const id = identity(entry),
        prior = localRef.current.get(id),
        remote = entry.reading;
      if (prior && remote && remote.generation >= prior.generation) {
        const ranges = mergeReadRanges([...prior.ranges, ...remote.ranges]);
        const merged =
          remote.generation > prior.generation
            ? remote
            : {
                ...prior,
                ranges,
                header_read: prior.header_read || remote.header_read,
                explicit_read: prior.explicit_read || remote.explicit_read,
                unread: prior.unread && remote.unread,
              };
        if (JSON.stringify(prior) !== JSON.stringify(merged))
          updateLocal(id, merged);
      }
    },
    [updateLocal],
  );

  const enqueue = (id: string, observation: ReadObservation) => {
    const prior = queue.current.get(id);
    if (prior && prior.generation === observation.generation) {
      observation = {
        ...observation,
        header: prior.header || observation.header,
        ranges: mergeReadRanges([...prior.ranges, ...observation.ranges]),
      };
    }
    queue.current.set(id, observation);
  };

  const flush = useCallback(async () => {
    const controller = active.current;
    if (
      !controller ||
      controller.signal.aborted ||
      sending.current ||
      retryBlocked.current ||
      !queue.current.size
    )
      return;
    sending.current = true;
    const pending = [...queue.current.entries()].slice(0, 100);
    pending.forEach(([id]) => queue.current.delete(id));
    try {
      const result = await writeReading(
        flowId,
        "observations",
        { items: pending.map(([, item]) => item) },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const current = accept(result);
      if (current)
        for (const [id, item] of pending) {
          const entry = registry.current.get(id);
          if (!entry) continue;
          const prior = localRef.current.get(id) ?? entry.reading ?? fresh();
          if (prior.generation !== item.generation) continue;
          const ranges = mergeReadRanges([...prior.ranges, ...item.ranges]);
          const header_read = prior.header_read || item.header;
          const complete =
            entry.body_length === 0 ||
            (ranges.length === 1 &&
              ranges[0][0] === 0 &&
              ranges[0][1] === entry.body_length);
          updateLocal(id, {
            ...prior,
            ranges,
            header_read,
            unread: !(prior.explicit_read || (header_read && complete)),
          });
        }
      setError("");
    } catch (failure) {
      if (controller.signal.aborted) return;
      const conflict = failure instanceof ApiError && failure.status === 409;
      if (!conflict) pending.forEach(([id, item]) => enqueue(id, item));
      else pending.forEach(([id]) => suppressed.current.add(id));
      retryBlocked.current = true;
      setError(
        conflict ? "阅读状态已在其他页面变化，请刷新后继续" : "阅读状态未同步",
      );
    } finally {
      sending.current = false;
    }
  }, [flowId, accept, updateLocal]);

  const refresh = useCallback(
    async (restorePosition = false) => {
      const controller = active.current;
      if (!controller) return;
      try {
        const next = await getReading(flowId, controller.signal);
        if (controller.signal.aborted) return;
        accept(next);
        if (restorePosition && next.anchor) {
          ready.current = false;
          const result = await getReadingResume(flowId, controller.signal);
          if (controller.signal.aborted) return;
          accept(result.state);
          setResume(result);
          restoreRef.current(result);
        }
        if (restorePosition) ready.current = true;
        setError("");
      } catch {
        if (!controller.signal.aborted) setError("阅读状态未同步");
      }
    },
    [flowId, accept],
  );

  const capture = (): ReadingAnchor | null => {
    const viewport = root.current;
    if (!viewport) return null;
    const nodes = [
      ...viewport.querySelectorAll<HTMLElement>("[data-read-start]"),
      ...viewport.querySelectorAll<HTMLElement>("[data-reading-header]"),
    ];
    const node = nodes.find((element) =>
      visibleRegion(element, viewport, false),
    );
    const host = node?.closest<HTMLElement>("[data-read-id]");
    const entry = host && registry.current.get(host.dataset.readId ?? "");
    if (!node || !entry || !entry.read_revision) return null;
    const rect = node.getBoundingClientRect(),
      top = viewport.getBoundingClientRect().top;
    return {
      key: entry.key,
      version: entry.version,
      read_revision: entry.read_revision,
      snapshot: entry.snapshot,
      parent_key: entry.parent_key,
      part: node.dataset.readStart !== undefined ? "body" : "summary",
      body_offset: Number(node.dataset.readStart ?? 0),
      block_fraction: Math.max(
        0,
        Math.min(1, (top - rect.top) / Math.max(1, rect.height)),
      ),
      occurred_at: entry.occurred_at,
      layout_version: 1,
    };
  };

  const savePosition = async (takeOver = false) => {
    const controller = active.current;
    if (
      !controller ||
      controller.signal.aborted ||
      !stateRef.current ||
      !document.hasFocus() ||
      document.visibilityState !== "visible" ||
      (blocked.current && !takeOver) ||
      !capture()
    )
      return;
    if (positioning.current) {
      pendingPosition.current = true;
      return;
    }
    positioning.current = true;
    pendingPosition.current = false;
    let saved = false;
    try {
      if (!writer.current || takeOver) {
        // An explicit takeover fetches the current revision; ordinary input never retries a failed CAS.
        if (takeOver) accept(await getReading(flowId, controller.signal));
        const claimed = await writeReading(
          flowId,
          "position/claim",
          {
            session_id: session.current,
            position_revision: stateRef.current.position_revision,
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        accept(claimed);
        writer.current = { epoch: claimed.writer_epoch, sequence: 0 };
        blocked.current = false;
        setPositionConflict(false);
      }
      const anchor = capture();
      if (
        !anchor ||
        !document.hasFocus() ||
        document.visibilityState !== "visible"
      )
        return;
      const result = await writeReading(
        flowId,
        "position",
        {
          session_id: session.current,
          writer_epoch: writer.current.epoch,
          sequence: ++writer.current.sequence,
          anchor,
        },
        controller.signal,
      );
      if (!controller.signal.aborted) {
        accept(result);
        saved = true;
      }
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && failure.status === 409) {
        blocked.current = true;
        writer.current = null;
        setPositionConflict(true);
      } else setError("阅读位置未同步");
    } finally {
      positioning.current = false;
      // Coalesce navigation during a slow save, but never retry a failed writer
      // or save after the page lost focus. Capture the latest visible position.
      const again = saved && pendingPosition.current;
      pendingPosition.current = false;
      if (again) void saveRef.current();
    }
  };
  const saveRef = useRef(savePosition);
  saveRef.current = savePosition;

  useEffect(() => {
    const controller = new AbortController();
    active.current = controller;
    void refresh(true);
    const viewport = root.current;
    const seen = new Map<HTMLElement, { token: string; since: number }>();
    let positionTimer = 0;
    const input = (event: Event) => {
      if (!event.isTrusted || blocked.current) return;
      if (
        event instanceof KeyboardEvent &&
        ![
          "ArrowUp",
          "ArrowDown",
          "PageUp",
          "PageDown",
          "Home",
          "End",
          " ",
        ].includes(event.key)
      )
        return;
      if (
        event.type === "click" &&
        !(
          event.target instanceof Element &&
          event.target.closest("[data-reading-navigation]")
        )
      )
        return;
      clearTimeout(positionTimer);
      positionTimer = window.setTimeout(() => void saveRef.current(), 350);
    };
    for (const type of ["wheel", "touchmove", "keydown", "click"])
      viewport?.addEventListener(type, input, { passive: true });
    const unfocus = () => {
      seen.clear();
      clearTimeout(positionTimer);
      pendingPosition.current = false;
    };
    const focus = () => void refresh();
    window.addEventListener("blur", unfocus);
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", unfocus);
    const timer = window.setInterval(() => {
      if (
        !viewport ||
        !ready.current ||
        previewing.current ||
        !document.hasFocus() ||
        document.visibilityState !== "visible"
      ) {
        seen.clear();
        return;
      }
      const present = new Set<HTMLElement>(),
        visibleIds = new Set<string>();
      for (const element of viewport.querySelectorAll<HTMLElement>(
        "[data-reading-header], [data-read-start]",
      )) {
        const host = element.closest<HTMLElement>("[data-read-id]");
        const id = host?.dataset.readId;
        const entry = id ? registry.current.get(id) : undefined;
        if (
          !id ||
          !entry?.read_revision ||
          !visibleRegion(element, viewport, false)
        )
          continue;
        visibleIds.add(id);
        if (suppressed.current.has(id) || !visibleRegion(element, viewport))
          continue;
        present.add(element);
        const value = localRef.current.get(id) ?? entry.reading ?? fresh();
        const header = element.dataset.readingHeader !== undefined;
        const span = header
          ? []
          : [
              [
                Number(element.dataset.readStart),
                Number(element.dataset.readEnd),
              ],
            ];
        const token = JSON.stringify([id, value.generation, header, span]);
        if (emitted.current.has(token) || !value.unread) continue;
        if (seen.get(element)?.token !== token)
          seen.set(element, { token, since: performance.now() });
        if (performance.now() - seen.get(element)!.since < 600) continue;
        emitted.current.add(token);
        enqueue(id, {
          key: entry.key,
          version: entry.version,
          read_revision: entry.read_revision,
          generation: value.generation,
          header,
          ranges: span,
        });
      }
      for (const element of seen.keys())
        if (!present.has(element)) seen.delete(element);
      for (const id of suppressed.current)
        if (!visibleIds.has(id)) suppressed.current.delete(id);
      void flush();
    }, 100);
    return () => {
      controller.abort();
      ready.current = false;
      clearInterval(timer);
      clearTimeout(positionTimer);
      for (const type of ["wheel", "touchmove", "keydown", "click"])
        viewport?.removeEventListener(type, input);
      window.removeEventListener("blur", unfocus);
      window.removeEventListener("focus", focus);
      document.removeEventListener("visibilitychange", unfocus);
    };
  }, [flowId, root, refresh, flush]);

  useEffect(() => {
    // Refreshes can introduce unread activity outside the mounted window.
    // Fetching its summary must never generate a read receipt or a position claim.
    if (snapshot !== null) void refresh();
  }, [snapshot, refresh]);

  const markEntry = async (entry: ActivityItem, read: boolean) => {
    if (!stateRef.current || !entry.read_revision) return;
    const id = identity(entry),
      prior = localRef.current.get(id) ?? entry.reading ?? fresh();
    suppressed.current.add(id);
    queue.current.delete(id);
    try {
      const result = await writeReading(
        flowId,
        "mark",
        {
          action: read ? "entry_read" : "entry_unread",
          key: entry.key,
          version: entry.version,
          read_revision: entry.read_revision,
          generation: prior.generation,
          receipts_version: stateRef.current.receipts_version,
        },
        active.current?.signal,
      );
      if (active.current?.signal.aborted) return;
      if (accept(result))
        updateLocal(id, {
          ...fresh(),
          generation: prior.generation + (read ? 0 : 1),
          explicit_read: read,
          header_read: read,
          unread: !read,
        });
      setError("");
    } catch {
      if (!active.current?.signal.aborted)
        setError("阅读状态未同步，请刷新后重试标记");
    }
  };
  const markSnapshot = async () => {
    if (!stateRef.current || !preview) return;
    try {
      const result = await writeReading(
        flowId,
        "mark",
        {
          action: "snapshot_read",
          snapshot: preview.snapshot,
          receipts_version: stateRef.current.receipts_version,
        },
        active.current?.signal,
      );
      if (active.current?.signal.aborted) return;
      accept(result);
      for (const [id, entry] of registry.current)
        if (entry.version <= preview.snapshot) {
          const prior = localRef.current.get(id) ?? entry.reading ?? fresh();
          updateLocal(id, {
            ...prior,
            explicit_read: true,
            header_read: true,
            unread: false,
          });
        }
      setError("");
      setPreview(null);
      previewing.current = false;
    } catch {
      if (!active.current?.signal.aborted)
        setError("阅读状态未同步，请刷新后重新确认");
    }
  };
  return {
    state,
    error,
    positionConflict,
    resume,
    preview,
    context: {
      anchor: resume?.resolved_anchor ?? null,
      snapshot: snapshot ?? 0,
      register,
      local,
      markEntry,
    },
    retry: () => {
      retryBlocked.current = false;
      if (queue.current.size) void flush();
      else void refresh(!ready.current);
    },
    restoreRemote: () => {
      writer.current = null;
      blocked.current = true;
      void refresh(true);
    },
    continueHere: () => void saveRef.current(true),
    markSnapshot,
    clearResume: () => setResume(null),
    cancelPreview: () => {
      setPreview(null);
      previewing.current = false;
    },
    prepareSnapshot: async () => {
      if (snapshot === null) return;
      previewing.current = true;
      try {
        const result = await getReading(
          flowId,
          active.current?.signal,
          snapshot,
        );
        if (!active.current?.signal.aborted) {
          setPreview(result);
          accept(result);
        }
      } catch {
        previewing.current = false;
        if (!active.current?.signal.aborted)
          setError("无法获取标记范围，请重试");
      }
    },
  };
}

export function ReadingProvider({
  value,
  children,
}: {
  value: ReadingContextValue;
  children: ReactNode;
}) {
  return <ReadingContext value={value}>{children}</ReadingContext>;
}

export function useReadingAnchor() {
  return useContext(ReadingContext)?.anchor ?? null;
}

/** Register identities separately from bodies/DOM, including replies and merged tool results. */
export function ReadingHeader({
  entry,
  children,
  headerOnControl = false,
}: {
  entry: ActivityItem;
  children: ReactNode;
  headerOnControl?: boolean;
}) {
  const context = useContext(ReadingContext);
  useEffect(() => {
    context?.register(entry, context.snapshot);
  }, [context?.register, context?.snapshot, entry]);
  const value = context?.local.get(identity(entry)) ?? entry.reading;
  return (
    <div data-read-id={identity(entry)}>
      {children && (
        <div data-reading-header={headerOnControl ? undefined : ""}>
          {children}
        </div>
      )}
      {context && entry.read_revision && (
        <button
          type="button"
          data-reading-header={!children || headerOnControl ? "" : undefined}
          className="reading-entry"
          onClick={() => void context.markEntry(entry, value?.unread !== false)}
        >
          {value?.unread === false ? "已读 · 标为未读" : "未读 · 标为已读"}
        </button>
      )}
    </div>
  );
}

export function readingAttributes(entry: ActivityItem) {
  return { "data-read-id": identity(entry) };
}

export function PlainReadingText({
  text,
  offset,
}: {
  text: string;
  offset: number;
}) {
  const chars = Array.from(text),
    spans: ReactNode[] = [];
  for (let start = 0; start < chars.length; start += 24)
    spans.push(
      <span
        key={start}
        data-read-start={offset + start}
        data-read-end={offset + Math.min(start + 24, chars.length)}
      >
        {chars.slice(start, start + 24).join("")}
      </span>,
    );
  return spans;
}
