import {
  createContext,
  type ReactNode,
  type ComponentProps,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { defaultRangeExtractor, useVirtualizer } from "@tanstack/react-virtual";
import { focusableControls } from "./focus";

export const MAX_ROWS = 60;
export const TEXT_CHARS = 2048;
export const CODE_CHARS = 8192;
export const CODE_LINES = 200;
const Memory = createContext(new Map<string, number>());

/** Reading state is separate from mounted DOM and from evictable activity pages. */
export function ReadingState({ children }: { children: ReactNode }) {
  const store = useRef(new Map<string, number>());
  return <Memory value={store.current}>{children}</Memory>;
}

function useReadingState(key: string, initial: number) {
  const store = useContext(Memory);
  const [value, setValue] = useState(() => store.get(key) ?? initial);
  useEffect(() => setValue(store.get(key) ?? initial), [key, initial, store]);
  const update = (next: number) => {
    store.set(key, next);
    setValue(next);
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

interface WindowProps {
  count: number;
  itemKey: (index: number) => string | number;
  children: (index: number) => ReactNode;
  estimate?: number;
  onRange?: (indexes: number[]) => void;
  label: string;
}

/** Small collections stay in normal flow; growing collections share the page's scroll root. */
export function WindowList({
  count,
  itemKey,
  children,
  estimate = 180,
  onRange,
  label,
}: WindowProps) {
  const large = count > MAX_ROWS;
  const host = useRef<HTMLDivElement>(null);
  const measured = useRef(new Map<string | number, number>());
  const [root, setRoot] = useState<HTMLElement | null>(null);
  const [margin, setMargin] = useState(0);
  const [focused, setFocused] = useState<number | null>(null);
  const [pending, setPending] = useState<{
    index: number;
    reverse: boolean;
  } | null>(null);
  const virtual = useVirtualizer<HTMLElement, HTMLDivElement>({
    count,
    enabled: large,
    // Let React batch range updates instead of forcing layout changes during size delivery.
    useFlushSync: false,
    getScrollElement: () => (large ? root : null),
    initialOffset: () => root?.scrollTop ?? 0,
    estimateSize: (index) => measured.current.get(itemKey(index)) ?? estimate,
    getItemKey: itemKey,
    overscan: 3,
    scrollMargin: margin,
    rangeExtractor: (range) => {
      const pins = [
        ...new Set(
          [focused, pending?.index].filter(
            (n): n is number => n !== null && n !== undefined && n < count,
          ),
        ),
      ];
      const visible = defaultRangeExtractor(range).slice(
        0,
        MAX_ROWS - pins.length,
      );
      return [...new Set([...visible, ...pins])].sort((a, b) => a - b);
    },
  });
  useLayoutEffect(() => {
    setRoot(host.current?.closest<HTMLElement>("[data-scroll-root]") ?? null);
  }, []);
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
        if (target instanceof HTMLElement) {
          const index = Number(target.dataset.windowRow);
          measured.current.set(
            itemKey(index),
            target.getBoundingClientRect().height,
          );
        }
      }
    });
    for (const row of host.current.children) observer.observe(row);
    return () => observer.disconnect();
  }, [count, itemKey]);
  const rows = large
    ? virtual.getVirtualItems()
    : Array.from({ length: count }, (_, index) => ({
        index,
        key: itemKey(index),
        start: 0,
      }));
  const indexes = rows.map(({ index }) => index).join(",");
  useEffect(() => {
    if (indexes) onRange?.(indexes.split(",").map(Number));
  }, [indexes, onRange]);
  useLayoutEffect(() => {
    if (!pending) return;
    const row = host.current?.querySelector<HTMLElement>(
      `:scope > [data-window-row="${pending.index}"]`,
    );
    const controls = focusableControls(row);
    const target = pending.reverse
      ? (controls.at(-1) ?? row)
      : (controls[0] ?? row);
    if (target) {
      target.focus({ preventScroll: true });
      setPending(null);
    }
  }, [indexes, pending]);
  const ownRow = (target: EventTarget | null) => {
    if (!(target instanceof HTMLElement)) return null;
    let node = target;
    while (node.parentElement && node.parentElement !== host.current)
      node = node.parentElement;
    return node.parentElement === host.current
      ? Number(node.dataset.windowRow)
      : null;
  };
  return (
    <div
      className={`window-list${large ? " virtual-list" : ""}`}
      data-window-list={label}
      data-window-count={count}
      ref={host}
      style={
        large
          ? { height: virtual.getTotalSize(), position: "relative" }
          : undefined
      }
      onFocusCapture={(event) => setFocused(ownRow(event.target))}
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
        const next = index + (event.shiftKey ? -1 : 1);
        if (event.target !== edge || next < 0 || next >= count) return;
        // Native Tab would skip every unmounted record between two DOM windows.
        event.preventDefault();
        event.stopPropagation();
        setPending({ index: next, reverse: event.shiftKey });
        virtual.scrollToIndex(next, { align: "auto" });
      }}
    >
      {rows.map((row) => (
        <div
          key={row.key}
          data-index={row.index}
          data-window-row={row.index}
          tabIndex={-1}
          ref={(node) => {
            if (large) virtual.measureElement(node);
            else if (node)
              measured.current.set(
                itemKey(row.index),
                node.getBoundingClientRect().height,
              );
          }}
          style={
            large
              ? {
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${row.start - margin}px)`,
                }
              : undefined
          }
        >
          {children(row.index)}
        </div>
      ))}
    </div>
  );
}
