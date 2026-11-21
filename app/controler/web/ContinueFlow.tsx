import { useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { createFailure, createFlow } from "./api";
import { Button } from "./components/ui/button";
import { FlowTaskInput } from "./FlowTaskInput";
import { taskContentFromText } from "./flowInput";
import type { FlowDetail } from "./types";

/** Continue a saved source without changing its repository, installation or Issue identity. */
export function ContinueFlow({
  source,
  onCreated,
}: {
  source: FlowDetail;
  onCreated: (id: number) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const writing = useRef(false);
  const [task, setTask] = useState(source.task_content ?? "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const { installation_id, issue_number } = source;
  if (installation_id == null)
    return <span>缺少仓库授权信息，无法创建新 Flow。</span>;
  const content = taskContentFromText(task);
  const input =
    issue_number !== null
      ? { issue_number }
      : content.ok
        ? { task_content: content.task }
        : null;
  const submit = async () => {
    if (writing.current || !input) return;
    writing.current = true;
    setPending(true);
    setError("");
    try {
      const created = await createFlow({
        repo: source.repo,
        installation_id,
        continued_from_flow_id: source.id,
        ...input,
      });
      // Navigating away while the request is in flight must not reopen the old page.
      if (dialog.current) {
        dialog.current.close();
        onCreated(created.id);
      }
    } catch (cause) {
      const failure = createFailure(cause);
      setError(
        failure.unknown
          ? "未收到创建结果。可再次提交以打开同一来源的 Flow。"
          : failure.message,
      );
    } finally {
      writing.current = false;
      setPending(false);
    }
  };
  return (
    <>
      <Button className="btn" onClick={() => dialog.current?.showModal()}>
        <Plus />
        创建新 Flow 继续
      </Button>
      <dialog
        ref={dialog}
        className="create-flow-dialog"
        aria-labelledby="continue-heading"
        onCancel={(event) => {
          if (writing.current) event.preventDefault();
        }}
      >
        <form
          aria-busy={pending}
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <h2 id="continue-heading">创建新 Flow 继续</h2>
          <p className="create-flow-intro">
            来源：Flow #{source.id} · {source.repo}
            {issue_number !== null && ` · Issue #${issue_number}`}
            。原有记录不会改变。
          </p>
          {issue_number === null && (
            <>
              <label htmlFor="continue-task">任务内容</label>
              <FlowTaskInput
                id="continue-task"
                autoFocus
                required
                rows={7}
                disabled={pending}
                value={task}
                onValueChange={setTask}
              />
              {!content.ok && (
                <p className="create-flow-hint">{content.error}</p>
              )}
            </>
          )}
          {error && (
            <p className="create-flow-error" role="alert">
              {error}
            </p>
          )}
          <div className="create-flow-actions">
            <Button
              type="button"
              className="btn"
              aria-label="取消"
              title="取消"
              disabled={pending}
              onClick={() => dialog.current?.close()}
            >
              <X />
            </Button>
            <Button
              type="submit"
              className="btn create-flow-submit"
              disabled={pending || !input}
            >
              <Plus />
              {pending ? "正在开启 Flow…" : "开启 Flow"}
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}
