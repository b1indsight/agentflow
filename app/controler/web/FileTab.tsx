import { FileText, X } from "lucide-react";
import { TabsTrigger } from "./components/ui/tabs";
import type { WorkerFile } from "./files";

/** A named document tab with a separate close control in either tab strip. */
export function FileTab({
  file,
  duplicate,
  side,
  close,
}: {
  file: WorkerFile;
  duplicate: boolean;
  side: boolean;
  close: () => void;
}) {
  const value = `file-${file.id}`;
  const filename = file.name ?? file.id.slice(0, 8);
  const suffix = duplicate ? ` ${file.id.slice(0, 6)}` : "";
  return (
    <div
      className="file-tab"
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
