import { useEffect, useRef, useState, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "./components/ui/button";
import { fileFailureMessage, previewFile, readFile } from "./files";

type ImageState =
  | { kind: "loading" }
  | { kind: "ready"; url: string }
  | { kind: "error"; message: string };

/** Read validated Worker markers near the viewport; never load an Amp URL as an image source. */
export function AmpImage({
  id,
  ampUrl,
  alt,
  title,
  children,
}: {
  id: string;
  ampUrl: string;
  alt: string;
  title?: string;
  children?: ReactNode;
}) {
  const container = useRef<HTMLSpanElement>(null);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ImageState>({ kind: "loading" });
  useEffect(() => {
    const node = container.current;
    if (!node) return;
    const request = new AbortController();
    setState({ kind: "loading" });
    const observer = new IntersectionObserver(
      (entries) => {
        if (
          request.signal.aborted ||
          !entries.some((entry) => entry.isIntersecting)
        )
          return;
        observer.disconnect();
        void readFile(id, request.signal)
          .then(previewFile)
          .then((preview) => {
            // Decoding is asynchronous too; cancelled or replaced images cannot publish late results.
            if (request.signal.aborted) return;
            setState(
              preview.kind === "image"
                ? { kind: "ready", url: preview.url }
                : { kind: "error", message: "图片无法解码或不是支持的位图。" },
            );
          })
          .catch((error: Error) => {
            if (!request.signal.aborted)
              setState({ kind: "error", message: fileFailureMessage(error) });
          });
      },
      { root: node.closest("[data-scroll-root]"), rootMargin: "600px 0px" },
    );
    observer.observe(node);
    return () => {
      observer.disconnect();
      request.abort();
    };
  }, [id, ampUrl, attempt]);
  return (
    <span
      ref={container}
      className="amp-image"
      aria-busy={state.kind === "loading"}
    >
      <a
        href={ampUrl}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={alt}
        title={title}
      >
        {children ?? alt}
        {state.kind === "ready" && (
          <img
            src={state.url}
            alt={alt}
            title={title}
            referrerPolicy="no-referrer"
          />
        )}
      </a>
      {state.kind === "loading" && <span role="status">正在读取图片…</span>}
      {state.kind === "error" && (
        <span className="amp-image-error">
          <span role="alert">{state.message}</span>
          <Button
            variant="ghost"
            className="btn ghost"
            aria-label="重试图片"
            title="重试图片"
            onClick={() => setAttempt((value) => value + 1)}
          >
            <RefreshCw aria-hidden="true" />
          </Button>
        </span>
      )}
    </span>
  );
}
