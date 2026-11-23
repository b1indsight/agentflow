import {
  type ComponentProps,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { ImagePlus, X } from "lucide-react";
import { replaceTaskText, updateTaskText } from "./flowInput";

function Thumbnail({ file }: { file: File }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    const url = URL.createObjectURL(file);
    setSrc(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  return <img src={src || undefined} alt={file.name} />;
}

/** Task text shared by Flow creation and appended Agent tasks, preserving pasted line endings. */
export function FlowTaskInput({
  value,
  files,
  onFilesChange,
  onValueChange,
  onSubmitShortcut,
  ...props
}: Omit<
  ComponentProps<"textarea">,
  "value" | "onChange" | "onPaste" | "onKeyDown"
> & {
  value: string;
  files: File[];
  onFilesChange: (files: File[]) => void;
  onValueChange: (value: string) => void;
  onSubmitShortcut?: () => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const drag = useRef<{ y: number; height: number } | null>(null);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  function addFiles(selected: File[]) {
    if (!selected.length || props.disabled) return;
    if (
      selected.some(
        (file) =>
          !["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
          file.size > 1048576,
      ) ||
      files.length + selected.length > 4
    ) {
      setError("最多添加 4 张 PNG、JPEG 或 WebP 图片，每张不超过 1 MiB。");
      return;
    }
    setError("");
    onFilesChange([...files, ...selected]);
  }
  function resize(height: number) {
    const el = textarea.current!;
    const min = parseFloat(getComputedStyle(el).minHeight);
    const dialog = el.closest("dialog");
    if (dialog && !dialog.style.marginBottom) {
      dialog.style.marginBottom = `${window.innerHeight - dialog.getBoundingClientRect().bottom}px`;
    }
    el.style.height = `${Math.max(min, Math.min(window.innerHeight * 0.55, height))}px`;
  }
  const pastedCursor = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (pastedCursor.current !== null) {
      textarea.current?.setSelectionRange(
        pastedCursor.current,
        pastedCursor.current,
      );
      pastedCursor.current = null;
    }
  }, [value]);

  return (
    <div
      className="task-composer"
      data-dragging={dragging || undefined}
      onDragOver={(event) => {
        if (event.dataTransfer.types.includes("Files")) {
          event.preventDefault();
          setDragging(!props.disabled);
        }
      }}
      onDragLeave={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null))
          setDragging(false);
      }}
      onDrop={(event) => {
        if (!event.dataTransfer.types.includes("Files")) return;
        event.preventDefault();
        setDragging(false);
        addFiles(Array.from(event.dataTransfer.files));
      }}
    >
      <button
        type="button"
        className="input-grip"
        aria-label="调整输入框高度"
        title="上拉扩大，下拉缩小；也可使用上下方向键"
        disabled={props.disabled}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          drag.current = {
            y: event.clientY,
            height: textarea.current!.offsetHeight,
          };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (
            drag.current &&
            event.currentTarget.hasPointerCapture(event.pointerId)
          )
            resize(drag.current.height + drag.current.y - event.clientY);
        }}
        onLostPointerCapture={() => {
          drag.current = null;
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp" || event.key === "ArrowDown") {
            event.preventDefault();
            resize(
              textarea.current!.offsetHeight +
                (event.key === "ArrowUp" ? 24 : -24),
            );
          }
        }}
      />
      <textarea
        {...props}
        ref={textarea}
        value={value.replace(/\r\n?/g, "\n")}
        onChange={(event) =>
          onValueChange(
            updateTaskText(
              value,
              event.target.value,
              event.target.selectionStart,
            ),
          )
        }
        onPaste={(event) => {
          // Native paste normalizes CRLF before onChange can observe it.
          event.preventDefault();
          const input = event.currentTarget;
          const pasted = event.clipboardData.getData("text/plain");
          const next = replaceTaskText(
            value,
            input.selectionStart,
            input.selectionEnd,
            pasted,
          );
          const cursor =
            input.selectionStart + pasted.replace(/\r\n?/g, "\n").length;
          if (next === value) input.setSelectionRange(cursor, cursor);
          else pastedCursor.current = cursor;
          onValueChange(next);
        }}
        onKeyDown={(event) => {
          if (
            onSubmitShortcut &&
            event.key === "Enter" &&
            (event.ctrlKey || event.metaKey) &&
            !event.nativeEvent.isComposing &&
            event.nativeEvent.keyCode !== 229
          ) {
            event.preventDefault();
            onSubmitShortcut();
          }
        }}
      />
      <div className="input-images">
        <input
          type="file"
          hidden
          multiple
          accept="image/png,image/jpeg,image/webp"
          ref={picker}
          disabled={props.disabled}
          onChange={(event) => {
            addFiles(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
        <button
          className="btn"
          type="button"
          disabled={props.disabled}
          title="选择图片或将图片拖入输入框"
          aria-label="添加图片"
          onClick={() => picker.current?.click()}
        >
          <ImagePlus aria-hidden="true" />
        </button>
        {files.map((file, index) => (
          <span className="image-preview" key={index}>
            <Thumbnail file={file} />
            <button
              type="button"
              className="btn"
              disabled={props.disabled}
              aria-label={`移除 ${file.name}`}
              onClick={() => {
                setError("");
                onFilesChange(files.filter((_, i) => i !== index));
              }}
            >
              <X aria-hidden="true" />
            </button>
          </span>
        ))}
      </div>
      {error && (
        <small role="alert" className="create-flow-error">
          {error}
        </small>
      )}
    </div>
  );
}
