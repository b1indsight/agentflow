import { useEffect, useMemo, useRef, useState } from "react";
import { Download, RefreshCw } from "lucide-react";
import { ApiError } from "./api";
import { Button } from "./components/ui/button";
import { readFile } from "./files";
import { parseSource, sourceLines } from "./fileSyntax";
import { CODE_LINES, textEnd, WindowList } from "./windowing";

type Preview =
  | { kind: "text"; text: string }
  | { kind: "image"; url: string }
  | { kind: "binary" };
type FileState =
  | { kind: "loading" }
  | { kind: "error"; error: Error }
  | { kind: "ready"; preview: Preview; size: number };

/** Only signed, decodable raster formats become images. Everything else is escaped UTF-8 or a download. */
async function previewFile(bytes: Uint8Array<ArrayBuffer>): Promise<Preview> {
  const begins = (...signature: number[]) =>
    signature.every((byte, i) => bytes[i] === byte);
  const header = new TextDecoder("ascii").decode(bytes.subarray(0, 12));
  const mime = begins(137, 80, 78, 71, 13, 10, 26, 10)
    ? "image/png"
    : begins(255, 216, 255)
      ? "image/jpeg"
      : header.startsWith("GIF87a") || header.startsWith("GIF89a")
        ? "image/gif"
        : header.startsWith("RIFF") && header.slice(8) === "WEBP"
          ? "image/webp"
          : null;
  if (mime) {
    try {
      const bitmap = await createImageBitmap(new Blob([bytes], { type: mime }));
      bitmap.close();
    } catch {
      return { kind: "binary" };
    }
    // Bounded data URLs use the existing image CSP, with no object URL lifetime or HTML/SVG execution.
    const chunks: string[] = [];
    for (let i = 0; i < bytes.length; i += 8192)
      chunks.push(String.fromCharCode(...bytes.subarray(i, i + 8192)));
    return {
      kind: "image",
      url: `data:${mime};base64,${btoa(chunks.join(""))}`,
    };
  }
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    if (!/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text))
      return { kind: "text", text };
  } catch {
    // Non-UTF-8 data is not lossy-decoded into misleading source text.
  }
  return { kind: "binary" };
}

function failureMessage(error: Error) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录后重试。";
    if (error.status === 403) return "没有文件读取权限，请核对登录身份。";
    if (error.status === 404)
      return "文件不存在或已被删除，请返回会话核对链接。";
    if (error.status === 409)
      return "文件已被替换，请返回会话获取重新登记的链接。";
    if (error.status === 413)
      return "文件超过 512 KiB 上限，无法预览或通过此接口下载。";
    const messages: Record<string, string> = {
      worker_unavailable: "Worker 离线或连接不可用，恢复连接后可重试。",
      query_busy: "文件读取繁忙，请稍后重试。",
      query_timeout: "Worker 读取超时，请重试。",
      file_unreadable: "Worker 无法读取文件，请核对文件权限后重试。",
      invalid_response: "Worker 返回的文件无效，请重试。",
      database_unavailable: "文件登记服务暂不可用，请重试。",
    };
    return `${messages[error.message] ?? "文件读取失败，请重试。"}（${error.message}）`;
  }
  return "文件读取失败，请检查网络或重新登录后重试。";
}

/** Reveal source in bounded steps; virtualize groups of physical lines so a wrapped line keeps one gutter. */
function FileSource({
  text,
  filename,
}: {
  text: string;
  filename: string | null;
}) {
  const tree = useMemo(() => parseSource(filename, text), [filename, text]);
  const [end, setEnd] = useState(() => textEnd(text, 0, true));
  const sentinel = useRef<HTMLDivElement>(null);
  const starts = useMemo(() => {
    const offsets = [0];
    let lines = 0;
    for (let i = 0; i < text.length - 1; i++)
      if (text[i] === "\n" && ++lines % CODE_LINES === 0) offsets.push(i + 1);
    return offsets;
  }, [text]);
  useEffect(() => {
    const node = sentinel.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) setEnd(textEnd(text, end, true));
      },
      { root: node.closest("[data-scroll-root]"), rootMargin: "0px 0px 160px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [text, end]);
  const next = starts.findIndex((start) => start > 0 && start >= end);
  return (
    <div className="numbered-code" tabIndex={0} aria-label="文件源码">
      <WindowList
        count={next < 0 ? starts.length : next}
        itemKey={(i) => starts[i]}
        label="文件源码行组"
        estimate={CODE_LINES * 23}
      >
        {(i) => {
          const lines = sourceLines(
            text,
            tree,
            starts[i],
            Math.min(end, starts[i + 1] ?? end),
          );
          return (
            <pre>
              <code>
                {lines.map((line, j) => (
                  <span className="code-line" key={j}>
                    <span
                      className="code-line-number"
                      aria-hidden="true"
                      data-line-number={i * CODE_LINES + j + 1}
                    />
                    <span className="code-line-text">{line}</span>
                  </span>
                ))}
              </code>
            </pre>
          );
        }}
      </WindowList>
      {end < text.length && (
        <div className="body-sentinel" ref={sentinel} aria-hidden="true" />
      )}
    </div>
  );
}

/** Only the active document is read; switching or retrying aborts and fences previous reads/decodes. */
export default function FileViewer({
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
          <p role="alert">{failureMessage(state.error)}</p>
        )}
        {authError && <a href="/console/">重新登录 / 刷新页面</a>}
        {state.kind === "ready" &&
          (state.preview.kind === "text" ? (
            <>
              {state.size === 0 && <p>空文件</p>}
              <FileSource text={state.preview.text} filename={filename} />
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
