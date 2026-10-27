import {
  createContext,
  type ReactNode,
  type ComponentProps,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { focusableControls } from "./focus";

export const MAX_ROWS = 60;
export const TEXT_CHARS = 2048;
export const CODE_CHARS = 8192;
export const CODE_LINES = 200;
function readingMemory() {
  return {
    values: new Map<string, number>(),
    listeners: new Map<string, Set<() => void>>(),
  };
}
const Memory = createContext(readingMemory());

/** Reading state is separate from mounted DOM and from evictable activity pages. */
export function ReadingState({ children }: { children: ReactNode }) {
  const store = useRef(readingMemory());
  return <Memory value={store.current}>{children}</Memory>;
}

export function useReadingState(key: string, initial: number) {
  const store = useContext(Memory);
  const value = useSyncExternalStore(
    (notify) => {
      const listeners = store.listeners.get(key) ?? new Set<() => void>();
      store.listeners.set(key, listeners);
      listeners.add(notify);
      return () => {
        listeners.delete(notify);
        if (!listeners.size) store.listeners.delete(key);
      };
    },
    () => store.values.get(key) ?? initial,
    () => initial,
  );
  const update = (next: number) => {
    store.values.set(key, next);
    store.listeners.get(key)?.forEach((notify) => notify());
  };
  return [value, update] as const;
}

export function SavedDetails({
  stateKey,
  open: initiallyOpen = false,
  ...props
}: ComponentProps<"details"> & { stateKey: string }) {
  const [open, setOpen] = useReadingState(stateKey, initiallyOpen ? 1 : 0);
  return (
    <details
      {...props}
      open={Boolean(open)}
      onToggle={(event) => {
        const next = event.currentTarget.open ? 1 : 0;
        if (next !== open) setOpen(next);
      }}
    />
  );
}

export function labelPreview(text: string, limit = 160) {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** One text window has a character budget as well as a code-line budget. */
export function textEnd(
  text: string,
  start: number,
  code: boolean,
  maxChars = code ? CODE_CHARS : TEXT_CHARS,
) {
  const limit = Math.min(text.length, start + maxChars);
  let end = limit;
  if (code) {
    let lines = 1;
    for (let i = start; i < limit; i++) {
      if (text[i] === "\n" && ++lines > CODE_LINES) {
        end = i;
        break;
      }
    }
  }
  // Never split a UTF-16 surrogate pair between windows.
  if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
  return end;
}

export function TextWindow({
  text,
  stateKey,
  code = false,
  label = "文本",
  maxChars,
}: {
  text: string;
  stateKey: string;
  code?: boolean;
  label?: string;
  maxChars?: number;
}) {
  const [saved, setStart] = useReadingState(stateKey, 0);
  const start = saved < text.length ? saved : 0;
  const end = textEnd(text, start, code, maxChars);
  const previous = () => {
    let before = 0;
    for (
      let next = 0;
      next < start;
      next = textEnd(text, next, code, maxChars)
    ) {
      before = next;
    }
    setStart(before);
  };
  const controls = (start > 0 || end < text.length) && (
    <span
      className="text-window-controls"
      role="group"
      aria-label={`${label}分段浏览`}
    >
      <button type="button" disabled={!start} onClick={previous}>
        上一段
      </button>
      <span>
        字符 {start + 1}–{end} / {text.length}
      </span>
      <button
        type="button"
        disabled={end === text.length}
        onClick={() => setStart(end)}
      >
        下一段
      </button>
    </span>
  );
  return code ? (
    <>
      <pre tabIndex={0} aria-label={label}>
        <code>{text.slice(start, end)}</code>
      </pre>
      {controls}
    </>
  ) : (
    <span className="text-window">
      {text.slice(start, end)}
      {controls}
    </span>
  );
}

type RowKey = string | number;
type RowGeometry = { top: number; height: number };
interface ReadingAnchor {
  key: RowKey;
  offset: number;
  scrollTop: number;
  rootTop: number;
  inputVersion: number;
}

interface WindowProps {
  count: number;
  itemKey: (index: number) => RowKey;
  children: (index: number) => ReactNode;
  estimate?: number;
  onRange?: (indexes: number[]) => void;
  onVisibleRange?: (keys: readonly RowKey[], anchorKey: RowKey | null) => void;
  preserveReadingAnchor?: boolean;
  label: string;
}

/** Mounted rows share normal flow; only omitted intervals use estimated heights. */
export function WindowList({
  count,
  itemKey,
  children,
  estimate = 180,
  onRange,
  onVisibleRange,
  preserveReadingAnchor = false,
  label,
}: WindowProps) {
  const large = count > MAX_ROWS;
  const host = useRef<HTMLDivElement>(null);
  const measured = useRef(new Map<RowKey, number>());
  const [root, setRoot] = useState<HTMLElement | null>(null);
  const [margin, setMargin] = useState(0);
  const [focused, setFocused] = useState<RowKey | null>(null);
  const [pending, setPending] = useState<{
    key: RowKey;
    reverse: boolean;
  } | null>(null);
  const [recovery, setRecovery] = useState<RowKey | null>(null);
  const layout = useRef({
    anchor: null as ReadingAnchor | null,
    geometry: new Map<RowKey, RowGeometry>(),
    keys: [] as RowKey[],
    inputVersion: 0,
    visible: [] as RowKey[],
    visibleAnchor: null as RowKey | null,
    reported: false,
  });
  const keys = Array.from({ length: count }, (_, index) => itemKey(index));
  const keyIndexes = new Map(keys.map((key, index) => [key, index]));
  const resolveKey = (key: RowKey | null) => {
    if (key === null || keyIndexes.has(key)) return key;
    const before = layout.current.keys;
    const index = before.indexOf(key);
    if (index < 0) return null;
    for (let i = index - 1; i >= 0; i--)
      if (keyIndexes.has(before[i])) return before[i];
    for (let i = index + 1; i < before.length; i++)
      if (keyIndexes.has(before[i])) return before[i];
    return null;
  };
  const focusedKey = resolveKey(focused);
  const pendingKey = resolveKey(pending?.key ?? null);
  const anchorKey = preserveReadingAnchor
    ? resolveKey(recovery ?? layout.current.anchor?.key ?? null)
    : null;
  const queued = useRef(false);
  const settle = useRef<() => void>(() => {});
  // Ancestor layout effects (header/tab positioning) finish before this batch reads geometry.
  const settleLayout = () => {
    if (queued.current) return;
    queued.current = true;
    queueMicrotask(() => {
      queued.current = false;
      settle.current();
    });
  };
  const virtual = useVirtualizer<HTMLElement, HTMLDivElement>({
    count,
    enabled: large,
    // Let React batch range updates instead of forcing layout changes during size delivery.
    useFlushSync: false,
    getScrollElement: () => (large ? root : null),
    initialOffset: () => root?.scrollTop ?? 0,
    // Switching a populated list into virtual mode must not unmount its focused row for one commit.
    initialRect: {
      width: root?.clientWidth ?? 0,
      height: root?.clientHeight ?? 0,
    },
    estimateSize: (index) => measured.current.get(itemKey(index)) ?? estimate,
    getItemKey: itemKey,
    overscan: 3,
    scrollMargin: margin,
    onChange: settleLayout,
    rangeExtractor: (range) => {
      const selected = new Set<number>();
      for (const key of [focusedKey, pendingKey, anchorKey]) {
        const index = key === null ? undefined : keyIndexes.get(key);
        if (index !== undefined) selected.add(index);
      }
      // Visible rows take priority over overscan; pins share this same hard budget.
      for (
        let index = range.startIndex;
        index <= range.endIndex && selected.size < MAX_ROWS;
        index++
      )
        selected.add(index);
      for (let distance = 1; distance <= range.overscan; distance++) {
        for (const index of [
          range.startIndex - distance,
          range.endIndex + distance,
        ])
          if (index >= 0 && index < count && selected.size < MAX_ROWS)
            selected.add(index);
      }
      return [...selected].sort((a, b) => a - b);
    },
  });
  // The library and the conversation must not both compensate one measurement.
  virtual.shouldAdjustScrollPositionOnItemSizeChange = preserveReadingAnchor
    ? () => false
    : undefined;

  settle.current = () => {
    const node = host.current;
    if (!node || !root) return;
    const before = layout.current;
    if (!preserveReadingAnchor && !onVisibleRange) {
      before.keys = keys;
      return;
    }
    const rootTop = root.getBoundingClientRect().top + root.clientTop;
    const visibleTop = Math.max(0, -rootTop);
    const visibleBottom = Math.min(root.clientHeight, innerHeight - rootTop);
    if (visibleBottom <= visibleTop) return;
    const geometry = new Map<RowKey, RowGeometry>();
    for (const row of node.querySelectorAll<HTMLElement>(
      ":scope > [data-window-row]",
    )) {
      const key = keys[Number(row.dataset.windowRow)];
      const rect = row.getBoundingClientRect();
      if (key === undefined) continue;
      geometry.set(key, {
        top: rect.top - rootTop + root.scrollTop,
        height: rect.height,
      });
      measured.current.set(key, rect.height);
    }
    const visibleRows = (positions: ReadonlyMap<RowKey, RowGeometry>) =>
      [...positions].filter(([, row]) => {
        const offset = row.top - root.scrollTop;
        return (
          row.height > 0 &&
          offset < visibleBottom &&
          offset + row.height > visibleTop
        );
      });
    const pickAnchor = (positions: ReadonlyMap<RowKey, RowGeometry>) => {
      const visible = visibleRows(positions);
      const first =
        visible.find(([, row]) => {
          const offset = row.top - root.scrollTop;
          return (
            offset >= visibleTop - 0.5 &&
            offset + row.height <= visibleBottom + 0.5
          );
        }) ?? visible[0];
      return first
        ? {
            key: first[0],
            offset: first[1].top - root.scrollTop,
            scrollTop: root.scrollTop,
            rootTop,
            inputVersion: before.inputVersion,
          }
        : null;
    };
    let anchor = before.anchor;
    if (preserveReadingAnchor && anchor) {
      const scrollMoved = Math.abs(root.scrollTop - anchor.scrollTop) > 0.5;
      const max = Math.max(0, root.scrollHeight - root.clientHeight);
      const clamped = anchor.scrollTop > max && root.scrollTop >= max - 0.5;
      const externalPosition =
        scrollMoved && (!clamped || Math.abs(rootTop - anchor.rootTop) > 0.5);
      if (anchor.inputVersion !== before.inputVersion || externalPosition) {
        // Read the latest input against the previous layout, not a request-start scroll position.
        anchor = pickAnchor(before.geometry);
      }
      const key = resolveKey(anchor?.key ?? null);
      if (anchor && key !== null) {
        anchor = { ...anchor, key };
        const row = geometry.get(key);
        if (!row) {
          before.anchor = anchor;
          if (recovery !== key) setRecovery(key);
          return;
        }
        // Using actual geometry also accounts for browser clamping after a group shrinks.
        const delta = row.top - root.scrollTop - anchor.offset;
        if (Math.abs(delta) > 0.5) root.scrollTop += delta;
      }
    }
    if (recovery !== null) setRecovery(null);
    before.anchor = pickAnchor(geometry);
    before.geometry = geometry;
    before.keys = keys;
    const visible = visibleRows(geometry).map(([key]) => key);
    const nextAnchor = before.anchor?.key ?? null;
    const bounds = node.getBoundingClientRect();
    const reliable =
      !count ||
      visible.length > 0 ||
      bounds.bottom <= rootTop + visibleTop ||
      bounds.top >= rootTop + visibleBottom;
    if (
      onVisibleRange &&
      reliable &&
      (!before.reported ||
        before.visibleAnchor !== nextAnchor ||
        before.visible.length !== visible.length ||
        visible.some((key, index) => key !== before.visible[index]))
    ) {
      before.reported = true;
      before.visible = visible;
      before.visibleAnchor = nextAnchor;
      onVisibleRange(visible, nextAnchor);
    }
  };
  useLayoutEffect(() => {
    setRoot(host.current?.closest<HTMLElement>("[data-scroll-root]") ?? null);
  }, []);
  useLayoutEffect(settleLayout);
  useLayoutEffect(() => {
    if (!root || (!preserveReadingAnchor && !onVisibleRange)) return;
    const input = () => {
      layout.current.inputVersion++;
    };
    const navigation = () => {
      input();
      settleLayout();
    };
    root.addEventListener("wheel", input, { passive: true });
    root.addEventListener("pointerdown", input, { passive: true });
    root.addEventListener("keydown", input);
    root.addEventListener("window-list-navigation", navigation);
    root.addEventListener("scroll", settleLayout, { passive: true });
    window.addEventListener("resize", settleLayout);
    return () => {
      root.removeEventListener("wheel", input);
      root.removeEventListener("pointerdown", input);
      root.removeEventListener("keydown", input);
      root.removeEventListener("window-list-navigation", navigation);
      root.removeEventListener("scroll", settleLayout);
      window.removeEventListener("resize", settleLayout);
    };
  }, [root, preserveReadingAnchor, Boolean(onVisibleRange)]);
  useLayoutEffect(() => {
    if (!large || !root || !host.current) return;
    const update = () => {
      if (!host.current) return;
      const next =
        root.scrollTop +
        host.current.getBoundingClientRect().top -
        root.getBoundingClientRect().top -
        root.clientTop;
      setMargin((before) => (Math.abs(before - next) > 0.5 ? next : before));
    };
    const observer = new ResizeObserver(update);
    let node: HTMLElement | null = host.current;
    while (node) {
      observer.observe(node);
      if (node === root) break;
      node = node.parentElement;
    }
    root.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
    return () => {
      observer.disconnect();
      root.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, [root, large]);
  useLayoutEffect(() => {
    if (count > MAX_ROWS || !host.current) return;
    const observer = new ResizeObserver((entries) => {
      for (const { target } of entries) {
        if (
          target instanceof HTMLElement &&
          target.hasAttribute("data-window-row")
        ) {
          const index = Number(target.dataset.windowRow);
          measured.current.set(
            itemKey(index),
            target.getBoundingClientRect().height,
          );
        }
      }
      settleLayout();
    });
    for (const row of host.current.querySelectorAll(
      ":scope > [data-window-row]",
    ))
      observer.observe(row);
    return () => observer.disconnect();
  }, [count, itemKey]);
  const rows = large
    ? virtual.getVirtualItems()
    : Array.from({ length: count }, (_, index) => ({
        index,
        key: itemKey(index),
        start: 0,
        end: 0,
      }));
  const indexes = rows.map(({ index }) => index).join(",");
  useEffect(() => {
    if (indexes) onRange?.(indexes.split(",").map(Number));
  }, [indexes, onRange]);
  useLayoutEffect(() => {
    if (focused !== focusedKey) {
      setFocused(focusedKey);
      const index =
        focusedKey === null ? undefined : keyIndexes.get(focusedKey);
      host.current
        ?.querySelector<HTMLElement>(`:scope > [data-window-row="${index}"]`)
        ?.focus({ preventScroll: true });
    }
    if (!pending) return;
    const index = pendingKey === null ? undefined : keyIndexes.get(pendingKey);
    if (index === undefined) {
      setPending(null);
      return;
    }
    const row = host.current?.querySelector<HTMLElement>(
      `:scope > [data-window-row="${index}"]`,
    );
    if (row && row.getBoundingClientRect().height === 0) {
      const next = index + (pending.reverse ? -1 : 1);
      if (next >= 0 && next < count) {
        setPending({ key: keys[next], reverse: pending.reverse });
        virtual.scrollToIndex(next, { align: "auto" });
      } else setPending(null);
      return;
    }
    const controls = focusableControls(row);
    const target = pending.reverse
      ? (controls.at(-1) ?? row)
      : (controls[0] ?? row);
    if (target) {
      target.focus({ preventScroll: true });
      setPending(null);
    }
  });
  const ownRow = (target: EventTarget | null) => {
    if (!(target instanceof HTMLElement)) return null;
    let node = target;
    while (node.parentElement && node.parentElement !== host.current)
      node = node.parentElement;
    return node.parentElement === host.current &&
      node.hasAttribute("data-window-row")
      ? Number(node.dataset.windowRow)
      : null;
  };
  const content: ReactNode[] = [];
  let previousEnd = margin;
  for (const row of rows) {
    if (large && row.start > previousEnd)
      content.push(
        <div
          key={`gap:${row.key}`}
          aria-hidden="true"
          style={{ height: row.start - previousEnd }}
        />,
      );
    content.push(
      <div
        key={`row:${row.key}`}
        data-index={row.index}
        data-window-row={row.index}
        tabIndex={-1}
        ref={(node) => {
          if (node)
            measured.current.set(
              keys[row.index],
              node.getBoundingClientRect().height,
            );
          if (large) virtual.measureElement(node);
        }}
      >
        {children(row.index)}
      </div>,
    );
    previousEnd = row.end;
  }
  const suffix = virtual.getTotalSize() + margin - previousEnd;
  if (large && suffix > 0)
    content.push(
      <div key="suffix" aria-hidden="true" style={{ height: suffix }} />,
    );
  return (
    <div
      className={`window-list${large ? " virtual-list" : ""}`}
      data-window-list={label}
      data-window-count={count}
      ref={host}
      onFocusCapture={(event) => {
        const index = ownRow(event.target);
        setFocused(index === null ? null : keys[index]);
      }}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget))
          setFocused(null);
      }}
      onKeyDown={(event) => {
        if (
          !large ||
          event.key !== "Tab" ||
          event.altKey ||
          event.ctrlKey ||
          event.metaKey
        )
          return;
        const index = ownRow(event.target);
        if (index === null) return;
        const row = host.current?.querySelector<HTMLElement>(
          `:scope > [data-window-row="${index}"]`,
        );
        const controls = focusableControls(row);
        const edge = event.shiftKey
          ? (controls[0] ?? row)
          : (controls.at(-1) ?? row);
        let next = index + (event.shiftKey ? -1 : 1);
        while (next >= 0 && next < count) {
          const candidate = host.current?.querySelector<HTMLElement>(
            `:scope > [data-window-row="${next}"]`,
          );
          if (!candidate || candidate.getBoundingClientRect().height > 0) break;
          next += event.shiftKey ? -1 : 1;
        }
        if (event.target !== edge || next < 0 || next >= count) return;
        // Native Tab would skip every unmounted record between two DOM windows.
        event.preventDefault();
        event.stopPropagation();
        layout.current.inputVersion++;
        setPending({ key: keys[next], reverse: event.shiftKey });
        virtual.scrollToIndex(next, { align: "auto" });
      }}
    >
      {content}
    </div>
  );
}
