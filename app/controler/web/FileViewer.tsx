import { useEffect, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { ApiError } from "./api";
import { Button } from "./components/ui/button";
import {
  fileFailureMessage,
  previewFile,
  readFile,
  type FilePreview,
} from "./files";
import { TextWindow } from "./windowing";

type FileState =
  | { kind: "loading" }
  | { kind: "error"; error: Error }
  | { kind: "ready"; preview: FilePreview; size: number };

/** Only the active document is read; switching or retrying aborts and fences previous reads/decodes. */
export function FileViewer({
  linkId,
  filename,
}: {
  linkId: string;
  filename: string | null;
}) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<FileState>({ kind: "loading" });
  useEffect(() => {
    const request = new AbortController();
    setState({ kind: "loading" });
    void readFile(linkId, request.signal)
      .then(async (bytes) => {
        const preview = await previewFile(bytes);
        if (!request.signal.aborted)
          setState({ kind: "ready", preview, size: bytes.byteLength });
      })
      .catch((error: Error) => {
        if (!request.signal.aborted) setState({ kind: "error", error });
      });
    return () => request.abort();
  }, [linkId, attempt]);
  const authError =
    state.kind === "error" &&
    state.error instanceof ApiError &&
    [401, 403].includes(state.error.status);
  return (
    <section
      className="file-viewer"
      aria-label="文件查看器"
      aria-busy={state.kind === "loading"}
    >
      <div className="file-toolbar">
        <div
          className="file-status"
          title="读取 Worker 当前内容，需在线；上限 512 KiB。HTML 和 SVG 仅显示源码。"
        >
          <span>只读</span>
          {state.kind === "ready" && (
            <>
              <span>
                {state.preview.kind === "text"
                  ? "UTF-8"
                  : state.preview.kind === "image"
                    ? "图片"
                    : "二进制"}
              </span>
              <span>{state.size.toLocaleString()} 字节</span>
            </>
          )}
        </div>
        <div className="file-actions">
          {state.kind === "ready" && (
            <Button asChild variant="ghost" className="btn ghost file-action">
              <a
                href={`/api/files/${linkId}`}
                download={filename ?? ""}
                rel="noreferrer"
                aria-label="下载原文件"
                title="下载原文件"
              >
                <Download aria-hidden="true" />
              </a>
            </Button>
          )}
          {state.kind !== "loading" && (
            <Button
              variant="ghost"
              className="btn ghost file-action"
              aria-label={state.kind === "error" ? "重试读取" : "重新读取"}
              title={state.kind === "error" ? "重试读取" : "重新读取"}
              onClick={() => setAttempt((n) => n + 1)}
            >
              <RefreshCw aria-hidden="true" />
            </Button>
          )}
        </div>
      </div>
      <div className="file-editor">
        {state.kind === "loading" && <p role="status">正在读取文件…</p>}
        {state.kind === "error" && (
          <p role="alert">{fileFailureMessage(state.error)}</p>
        )}
        {authError && <a href="/console/">重新登录 / 刷新页面</a>}
        {state.kind === "ready" &&
          (state.preview.kind === "text" ? (
            <>
              {state.size === 0 && <p>空文件</p>}
              <TextWindow
                text={state.preview.text}
                stateKey={`file-${linkId}-${attempt}`}
                code
                lineNumbers
                label="文件源码"
              />
            </>
          ) : state.preview.kind === "image" ? (
            <img
              className="file-image"
              src={state.preview.url}
              alt="文件图片预览"
            />
          ) : (
            <p>此二进制格式不支持预览，请下载原文件。</p>
          ))}
      </div>
    </section>
  );
}
