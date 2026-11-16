import { useEffect, useId, useRef, useState } from "react";
import { Send } from "lucide-react";

/** Shared instruction composer; callers own whether to create or continue a Flow. */
export function FlowInput({
  onSubmit,
  disabledReason,
}: {
  onSubmit: (message: string, requestId: string) => Promise<void>;
  disabledReason: string;
}) {
  const id = useId();
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef<{ message: string; id: string } | null>(null);
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const send = async () => {
    const text = message.trim();
    if (writing.current || disabledReason || !text) return;
    writing.current = true;
    setPending(true);
    setError("");
    setNotice("");
    try {
      // Reuse the identity after a lost response, even if the user switches tabs.
      if (request.current?.message !== text)
        request.current = { message: text, id: crypto.randomUUID() };
      await onSubmit(text, request.current.id);
      if (mounted.current) {
        setMessage("");
        request.current = null;
        setNotice("已发送，继续任务已排队。可在会话和执行记录中查看进展。");
      }
    } catch (error) {
      if (mounted.current)
        setError(
          error instanceof Error ? error.message : "发送失败，请刷新核对。",
        );
    } finally {
      writing.current = false;
      if (mounted.current) setPending(false);
    }
  };
  return (
    <form
      className="flow-input"
      aria-label="与 Agent 交流"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
    >
      <label htmlFor={id}>继续工作</label>
      <textarea
        id={id}
        aria-label="补充要求"
        aria-describedby={`${id}-hint`}
        placeholder="补充要求、回答问题，或告诉 Agent 下一步做什么…"
        rows={2}
        maxLength={16000}
        value={message}
        disabled={pending}
        onChange={(event) => {
          setMessage(event.target.value);
          setError("");
          setNotice("");
        }}
        onKeyDown={(event) => {
          if (
            event.key === "Enter" &&
            (event.ctrlKey || event.metaKey) &&
            !event.nativeEvent.isComposing &&
            event.nativeEvent.keyCode !== 229
          ) {
            event.preventDefault();
            void send();
          }
        }}
      />
      <div className="flow-input-actions">
        <small id={`${id}-hint`}>
          {disabledReason || "沿用当前 Flow · Ctrl / ⌘ + Enter 发送"}
        </small>
        <button
          type="submit"
          className="btn"
          disabled={pending || Boolean(disabledReason) || !message.trim()}
        >
          <Send />
          {pending ? "正在发送…" : "发送"}
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </form>
  );
}
