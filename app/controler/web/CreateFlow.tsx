import { useEffect, useId, useRef, useState } from "react";
import { Plus } from "lucide-react";
import { createFailure, createFlow, getFlowTargets } from "./api";
import { Button } from "./components/ui/button";
import { taskContentFromText } from "./flowInput";
import type { FlowTarget } from "./types";

const targetKey = (target: FlowTarget) =>
  `${target.installation_id}:${target.repo}`;

/** Drafts and uncertain writes survive closing; only confirmed creation clears the form. */
export function CreateFlow({
  onCreated,
  onVerify,
}: {
  onCreated: (id: number) => void;
  onVerify: () => void;
}) {
  const id = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const request = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const [targets, setTargets] = useState<FlowTarget[]>([]);
  const [selected, setSelected] = useState<FlowTarget | null>(null);
  const [body, setBody] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [next, setNext] = useState<string | null>(null);
  const [more, setMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState<{
    message: string;
    reset: boolean;
  } | null>(null);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<ReturnType<
    typeof createFailure
  > | null>(null);
  const content = taskContentFromText(body);
  const uncertain = failure?.unknown ?? false;
  const options =
    selected &&
    !targets.some((target) => targetKey(target) === targetKey(selected))
      ? [selected, ...targets]
      : targets;

  useEffect(() => () => request.current?.abort(), []);

  const read = async (reset = false) => {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setReadError(null);
    try {
      const page = await getFlowTargets(reset ? null : next, controller.signal);
      if (controller.signal.aborted) return;
      setTargets((prior) => {
        const merged = new Map(
          (reset ? [] : prior).map((target) => [targetKey(target), target]),
        );
        page.items.forEach((target) => merged.set(targetKey(target), target));
        return [...merged.values()];
      });
      setNext(page.next_cursor);
      setMore(page.has_more);
      setLoaded(true);
    } catch (error) {
      if (!controller.signal.aborted)
        setReadError({
          message: error instanceof Error ? error.message : "项目读取失败",
          reset,
        });
    } finally {
      if (request.current === controller) {
        request.current = null;
        setLoading(false);
      }
    }
  };
  const close = () => {
    if (!submitting.current) dialog.current?.close();
  };
  const submit = async () => {
    if (submitting.current || uncertain || !selected || !content.ok) return;
    // The synchronous guard also covers repeated Enter/click events before React renders disabled controls.
    submitting.current = true;
    setPending(true);
    setFailure(null);
    try {
      const flow = await createFlow(selected, content.task);
      setBody("");
      setSelected(null);
      dialog.current?.close();
      onCreated(flow.id);
    } catch (error) {
      setFailure(createFailure(error));
    } finally {
      submitting.current = false;
      setPending(false);
    }
  };

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="btn ghost"
        aria-label="新建 Flow"
        onClick={() => {
          dialog.current?.showModal();
          if (!loaded && !request.current) void read();
        }}
      >
        <Plus />
      </button>
      <dialog
        ref={dialog}
        className="create-flow-dialog"
        aria-labelledby={`${id}-heading`}
        onCancel={(event) => {
          if (submitting.current) event.preventDefault();
        }}
        onClose={() => trigger.current?.focus()}
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
          aria-busy={pending}
        >
          <h2 id={`${id}-heading`}>新建 Flow</h2>
          <p className="create-flow-intro">
            选择 GitHub 代码仓库，输入任务内容即可开启，无需 Issue。
          </p>
          <label htmlFor={`${id}-project`}>Project（代码仓库）</label>
          <select
            id={`${id}-project`}
            required
            autoFocus
            value={selected ? targetKey(selected) : ""}
            disabled={pending || uncertain}
            aria-describedby={`${id}-targets`}
            onChange={(event) => {
              setSelected(
                options.find(
                  (target) => targetKey(target) === event.target.value,
                ) ?? null,
              );
              setFailure(null);
            }}
          >
            <option value="" disabled>
              请选择 owner/name
            </option>
            {options.map((target) => (
              <option key={targetKey(target)} value={targetKey(target)}>
                {target.repo}
              </option>
            ))}
          </select>
          <p id={`${id}-targets`} className="create-flow-hint" role="status">
            {loading
              ? "正在读取项目…"
              : !loaded
                ? "项目清单尚未读取完成。"
                : more
                  ? targets.length
                    ? `已读取 ${targets.length} 个项目，仍有更多项目。`
                    : "尚未读取到可用项目，请继续读取后续页面。"
                  : targets.length
                    ? `已读取全部 ${targets.length} 个项目。`
                    : "暂无可用项目。请检查 GitHub App 授权及仓库状态。"}
          </p>
          {selected &&
            loaded &&
            !loading &&
            !more &&
            !targets.some(
              (target) => targetKey(target) === targetKey(selected),
            ) && (
              <p className="create-flow-hint">
                保留了之前选择的项目，但当前清单中未找到；创建时将重新校验访问权限。
              </p>
            )}
          {readError && (
            <p className="create-flow-error" role="alert">
              项目读取失败：{readError.message}
              。可重试读取，或重新读取项目清单。
            </p>
          )}
          <div className="create-flow-read-actions">
            {(more || readError) && (
              <Button
                type="button"
                variant="outline"
                className="btn"
                disabled={loading || pending}
                onClick={() => void read(readError?.reset ?? false)}
              >
                {readError ? "重试读取项目" : "读取更多项目"}
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              className="btn"
              disabled={loading || pending}
              onClick={() => void read(true)}
            >
              重新读取项目
            </Button>
          </div>
          <label htmlFor={`${id}-body`}>任务内容</label>
          <textarea
            id={`${id}-body`}
            required
            rows={7}
            value={body}
            disabled={pending || uncertain}
            aria-invalid={Boolean(body && !content.ok)}
            aria-describedby={`${id}-body-hint${body && !content.ok ? ` ${id}-validation` : ""}`}
            placeholder="第一行描述任务，其余内容说明要求、背景和验收方式…"
            onChange={(event) => {
              setBody(event.target.value);
              setFailure(null);
            }}
          />
          <p id={`${id}-body-hint`} className="create-flow-hint">
            全文原样保存，最多 32 KiB UTF-8；首个非空白行的前 256
            个字符用作标题。
          </p>
          {body && !content.ok && (
            <p
              id={`${id}-validation`}
              className="create-flow-error"
              role="alert"
            >
              {content.error}
            </p>
          )}
          <div className="create-flow-preview">
            <span>标题预览</span>
            <p aria-label="标题预览">
              {content.ok ? content.task.title : "填写任务内容后显示"}
            </p>
          </div>
          {failure && (
            <p className="create-flow-error" role="alert">
              {failure.message}
            </p>
          )}
          {pending && (
            <p className="create-flow-hint" role="status">
              正在提交，请勿关闭；关闭表单不能撤销创建。
            </p>
          )}
          <div className="create-flow-actions">
            <Button
              type="button"
              variant="outline"
              className="btn"
              disabled={pending}
              onClick={close}
            >
              {uncertain ? "安全关闭" : "取消"}
            </Button>
            {uncertain && (
              <Button
                type="button"
                variant="outline"
                className="btn create-flow-verify"
                onClick={() => {
                  close();
                  onVerify();
                }}
              >
                关闭并清空筛选、刷新看板核对
              </Button>
            )}
            <Button
              type="submit"
              className="btn create-flow-submit"
              disabled={pending || uncertain || !selected || !content.ok}
            >
              {pending ? "正在开启 Flow…" : "开启 Flow"}
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}
