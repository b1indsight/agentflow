import { FileText, X } from "lucide-react";
import { useRef, type DragEvent, type ReactNode } from "react";
import { TabsTrigger } from "./components/ui/tabs";
import type { WorkerFile } from "./files";

/** Find an insertion slot in a visible strip, after its fixed navigation tabs. */
function dropSlot(event: DragEvent) {
  const list = (event.target as Element).closest<HTMLElement>(
    "[data-file-column]",
  );
  if (!list) return;
  const tabs = [...list.querySelectorAll<HTMLElement>(".file-tab")];
  const before = tabs.find((tab) => {
    const box = tab.getBoundingClientRect();
    return event.clientX < box.left + box.width / 2;
  });
  return {
    column: Number(list.dataset.fileColumn),
    before: before?.dataset.fileId,
    anchor: before ?? (list.lastElementChild as HTMLElement),
    edge: before ? "before" : "after",
  };
}

/** Only drags started on a file tab in this row may change its layout. */
export function FileTabRow({
  children,
  split,
  move,
}: {
  children: ReactNode;
  split: boolean;
  move: (file: string, column: number, before?: string) => void;
}) {
  const dragged = useRef<string | undefined>(undefined);
  const marker = useRef<HTMLElement | null>(null);
  function clearMarker() {
    if (marker.current) delete marker.current.dataset.dropEdge;
    marker.current = null;
  }
  function showSlot(event: DragEvent) {
    if (!dragged.current) return;
    clearMarker();
    const slot = dropSlot(event);
    if (!slot) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    marker.current = slot.anchor;
    slot.anchor.dataset.dropEdge = slot.edge;
  }
  return (
    <div
      className={split ? "split-tab-row" : undefined}
      onDragStart={(event) => {
        const file = (event.target as HTMLElement).closest<HTMLElement>(
          ".file-tab",
        )?.dataset.fileId;
        if (!file) return;
        dragged.current = file;
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("application/x-flow-file-tab", file);
      }}
      onDragEnter={showSlot}
      onDragOver={showSlot}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          clearMarker();
      }}
      onDragEnd={() => {
        clearMarker();
        dragged.current = undefined;
      }}
      onDrop={(event) => {
        const file = dragged.current;
        const slot = dropSlot(event);
        if (
          !file ||
          !slot ||
          event.dataTransfer.getData("application/x-flow-file-tab") !== file
        )
          return;
        event.preventDefault();
        clearMarker();
        dragged.current = undefined;
        move(file, slot.column, slot.before);
      }}
    >
      {children}
    </div>
  );
}

/** A named document tab with a separate close control in either tab strip. */
export function FileTab({
  file,
  duplicate,
  side,
  select,
  close,
}: {
  file: WorkerFile;
  duplicate: boolean;
  side: boolean;
  select: () => void;
  close: () => void;
}) {
  const value = `file-${file.id}`;
  const filename = file.name ?? file.id.slice(0, 8);
  const suffix = duplicate ? ` ${file.id.slice(0, 6)}` : "";
  return (
    <div
      className="file-tab"
      data-file-id={file.id}
      onFocus={(event) =>
        event.currentTarget.scrollIntoView({
          block: "nearest",
          inline: "nearest",
        })
      }
    >
      <TabsTrigger
        value={value}
        data-tab={value}
        draggable
        // Radix selects on mouse-down and on focus. Defer mouse activation to
        // click so a canceled drag of a background file leaves its pane alone.
        onMouseDownCapture={(event) => event.stopPropagation()}
        onFocus={(event) => {
          if (event.currentTarget.matches(":active")) event.preventDefault();
        }}
        onClick={select}
        title={filename}
        {...(side
          ? { id: `tab-${value}`, "aria-controls": `panel-${value}` }
          : {})}
        onKeyDown={(event) => {
          if (event.key === "Delete") {
            event.preventDefault();
            close();
          }
        }}
      >
        <FileText aria-hidden="true" />
        <span className="file-tab-name">{filename}</span>
        {suffix && <small>{suffix}</small>}
      </TabsTrigger>
      <button
        type="button"
        className="file-tab-close"
        aria-label={`关闭 ${filename}${suffix}`}
        tabIndex={-1}
        onMouseDown={(event) => event.preventDefault()}
        onClick={close}
      >
        <X aria-hidden="true" />
      </button>
    </div>
  );
}
