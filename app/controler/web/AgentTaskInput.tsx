import { useEffect, useId, useRef, useState } from "react";
import { Check, CircleAlert, LoaderCircle, Send } from "lucide-react";
import { FlowTaskInput } from "./FlowTaskInput";

const composerCopy = {
  active: {
    label: "补充要求",
    placeholder: "补充要求、回答问题，或告诉 Agent 下一步做什么…",
  },
  note: {
    label: "收尾留言",
    placeholder: "记录本次执行的收尾事项…",
  },
  archived: {
    label: "本次执行消息",
    placeholder: "本次执行已归档",
  },
};

/** Append an Agent task; task editing is shared with Flow creation. */
export function AgentTaskInput({
  onSubmit,
  mode,
  busy,
}: {
  onSubmit: (
    instruction: string,
    requestId: string,
    files: File[],
  ) => Promise<void>;
  mode: keyof typeof composerCopy;
  busy: boolean;
}) {
  const id = useId();
  const [instruction, setInstruction] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [status, setStatus] = useState<"idle" | "pending" | "sent" | "error">(
    "idle",
  );
  const [error, setError] = useState("");
  const { label, placeholder } = composerCopy[mode];
  const disabled = busy || status === "pending" || mode === "archived";
  const icon = {
    idle: Send,
    pending: LoaderCircle,
    sent: Check,
    error: CircleAlert,
  };
  const Icon = icon[status];
  const feedback = {
    idle: "发送",
    pending: "正在发送…",
    sent: "消息已记录",
    error: "重试发送",
  }[status];
  const request = useRef<{ instruction: string; id: string } | null>(null);
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const send = async () => {
    const text = instruction.trim();
    if (writing.current || disabled || (!text && !files.length)) return;
    writing.current = true;
    setStatus("pending");
    setError("");
    try {
      // Reuse the identity after a lost response, even if the user switches tabs.
      if (request.current?.instruction !== text)
        request.current = { instruction: text, id: crypto.randomUUID() };
      await onSubmit(text, request.current.id, files);
      if (mounted.current) {
        setInstruction("");
        setFiles([]);
        request.current = null;
        setStatus("sent");
      }
    } catch (error) {
      if (mounted.current) {
        setStatus("error");
        setError(
          error instanceof Error ? error.message : "发送失败，请刷新核对。",
        );
      }
    } finally {
      writing.current = false;
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
      <FlowTaskInput
        files={files}
        onFilesChange={(next) => {
          setFiles(next);
          request.current = null;
          setStatus("idle");
          setError("");
        }}
        aria-label={label}
        aria-describedby={`${id}-hint`}
        placeholder={placeholder}
        rows={2}
        maxLength={16000}
        value={instruction}
        disabled={disabled}
        onValueChange={(value) => {
          setInstruction(value);
          setError("");
          setStatus("idle");
        }}
        onSubmitShortcut={() => void send()}
      />
      <div className="flow-input-actions">
        <small id={`${id}-hint`}>Ctrl / ⌘ + Enter 发送</small>
        <button
          type="submit"
          className="btn"
          aria-label={feedback}
          title={error || feedback}
          data-state={status}
          disabled={disabled || (!instruction.trim() && !files.length)}
        >
          <Icon
            aria-hidden="true"
            className={
              status === "pending" ? "motion-safe:animate-spin" : undefined
            }
          />
        </button>
      </div>
      <span className="sr-only" role="status">
        {status === "idle" ? "" : error || feedback}
      </span>
    </form>
  );
}
