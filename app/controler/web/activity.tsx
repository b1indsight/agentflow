import { useEffect, useId, useRef, useState } from "react";
import { getContent, getDiff, getThread } from "./api";
import { DiffWindow } from "./DiffWindow";
import type { ActivityItem, ActivityPage, ContentPage } from "./types";
import { SavedDetails, TextWindow, useReadingState } from "./windowing";

/** Keep only one immutable body segment in memory; API offsets count Unicode code points. */
function ActivityBody({
  entry,
  flowId,
}: {
  entry: ActivityItem;
  flowId: number;
}) {
  const [savedStart, saveStart] = useReadingState(
    `${flowId}-${entry.key}-${entry.version}-body-offset`,
    0,
  );
  const initialStart = useRef(savedStart).current;
  const [segment, setSegment] = useState({
    text: entry.body,
    start: 0,
    next:
      Array.from(entry.body).length < entry.body_length
        ? Array.from(entry.body).length
        : null,
  });
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const read = async (offset: number) => {
    if (busy) return;
    const c = new AbortController();
    request.current = c;
    setBusy(true);
    setError("");
    try {
      const p = await getContent(
        flowId,
        entry.key,
        entry.version,
        offset,
        c.signal,
      );
      if (!c.signal.aborted) {
        setSegment({ text: p.text, start: offset, next: p.next_offset });
        saveStart(offset);
      }
    } catch (e) {
      if (!c.signal.aborted)
        setError(e instanceof Error ? e.message : "正文读取失败");
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  };
  // Virtualized rows can unmount; restore only the selected immutable segment.
  useEffect(() => {
    if (initialStart > 0) void read(initialStart);
    return () => request.current?.abort();
  }, [initialStart]);
  const tool = entry.kind === "tool_call" || entry.kind === "tool_result";
  const body = (
    <TextWindow
      text={segment.text}
      stateKey={`${entry.key}-${entry.version}-${segment.start}`}
      code={tool}
      label={tool ? "工具摘要" : "正文"}
    />
  );
  return (
    <>
      {tool ? (
        <SavedDetails
          className="tool"
          stateKey={`${entry.key}-${entry.version}-tool`}
        >
          <summary>⌘ {entry.title || entry.kind}</summary>
          {body}
        </SavedDetails>
      ) : (
        <p className="message-body">{body}</p>
      )}
      {(segment.start > 0 || segment.next !== null) && (
        <div className="text-window-controls">
          <button
            disabled={busy || segment.start === 0}
            onClick={() => void read(Math.max(0, segment.start - 16384))}
          >
            上一部分
          </button>
          <span>
            字符 {segment.start + 1}–
            {segment.start + Array.from(segment.text).length} /{" "}
            {entry.body_length}
          </span>
          <button
            disabled={busy || segment.next === null}
            onClick={() => segment.next !== null && void read(segment.next)}
          >
            下一部分
          </button>
        </div>
      )}
      {error && (
        <p role="alert">正文读取失败：{error}，请重试上一部分或下一部分。</p>
      )}
    </>
  );
}

/** Fetch the complete immutable comparison on demand; only its visible rows are mounted. */
function EditedDiff({
  entry,
  flowId,
}: {
  entry: ActivityItem;
  flowId: number;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<ContentPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const read = async () => {
    request.current?.abort();
    const c = new AbortController();
    request.current = c;
    setBusy(true);
    setError("");
    try {
      const result = await getDiff(flowId, entry.key, entry.version, c.signal);
      if (!c.signal.aborted) setPage(result);
    } catch (e) {
      if (!c.signal.aborted)
        setError(
          e instanceof Error && e.message === "diff_too_large"
            ? entry.url
              ? "内容过长，无法在此计算差异；可通过来源链接查看。"
              : "内容过长，无法在此计算差异；仍可分段查看正文。"
            : "差异读取失败，请重试。",
        );
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  };
  if (entry.previous_version === null || entry.deleted) return null;
  return (
    <div className="edit-history">
      <button
        type="button"
        className="edited-link"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          if (open) {
            request.current?.abort();
            setBusy(false);
          } else if (!page) void read();
          setOpen(!open);
        }}
      >
        已编辑
      </button>
      {open && (
        <section
          id={id}
          className="activity-diff"
          aria-label="与上一版的差异"
          aria-busy={busy}
        >
          <small>与上一版比较 · 删除为红色 −，新增为绿色 +</small>
          {busy && <p role="status">正在读取差异…</p>}
          {error && (
            <p role="alert">
              {error}{" "}
              <button type="button" onClick={() => void read()}>
                重试
              </button>
            </p>
          )}
          {page && (
            <DiffWindow
              text={page.text}
              stateKey={`${flowId}-${entry.key}-${entry.version}-diff`}
            />
          )}
        </section>
      )}
    </div>
  );
}

/** Replies replace their visible page, rather than accumulating an unbounded subtree. */
function ThreadReplies({
  entry,
  flowId,
  snapshot,
}: {
  entry: ActivityItem;
  flowId: number;
  snapshot: number;
}) {
  const [page, setPage] = useState<ActivityPage | null>(null);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);
  const read = async () => {
    if (busy) return;
    const c = new AbortController();
    request.current = c;
    setBusy(true);
    setError("");
    try {
      const cursor = page?.has_more ? page.next_cursor : null;
      const result = await getThread(
        flowId,
        entry.key,
        cursor,
        snapshot,
        c.signal,
      );
      if (!c.signal.aborted) setPage(result);
    } catch (e) {
      if (!c.signal.aborted)
        setError(e instanceof Error ? e.message : "关联记录读取失败");
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  };
  const label = "线程回复";
  return (
    <section className="activity-pages" aria-label={label}>
      <button className="btn" disabled={busy} onClick={() => void read()}>
        {page?.has_more ? `下一页${label}` : `查看${label}`}
      </button>
      {error && <p role="alert">{error}（点击上方按钮重试）</p>}
      {page?.items.map((item) => (
        <div key={`${item.key}-${item.version}`}>
          <p>
            {item.author}
            {item.deleted ? " · 已删除" : ""}
          </p>
          <ActivityBody
            key={`${item.key}-${item.version}`}
            entry={item}
            flowId={flowId}
          />
          <EditedDiff
            key={`diff-${item.key}-${item.version}`}
            entry={item}
            flowId={flowId}
          />
        </div>
      ))}
      {page && !page.has_more && (
        <small>
          已到{label}末尾 · {page.total} 条
        </small>
      )}
    </section>
  );
}

/** Untrusted content is React text, never repository-supplied HTML. */
export function ActivityView({
  entry,
  flowId = 0,
  snapshot = entry.version,
}: {
  entry: ActivityItem;
  flowId?: number;
  snapshot?: number;
}) {
  return (
    <article
      className={`message${entry.deleted ? " deleted" : ""}`}
      data-entry={entry.key}
    >
      <div className="message-avatar" aria-hidden="true">
        {entry.author.slice(0, 1).toUpperCase()}
      </div>
      <div className="message-main">
        <div className="message-label">
          <span className="message-identity">
            <span>{entry.author}</span>
            <span className="source-tag">{entry.source}</span>
          </span>
          {entry.prior && (
            <span
              className="message-context"
              title="发生于此 Flow 创建之前的历史内容"
            >
              此前交互
            </span>
          )}
          {entry.deleted && (
            <span className="message-context">已删除（保留历史版本）</span>
          )}
          <time>{new Date(entry.occurred_at).toLocaleString()}</time>
        </div>
        {entry.title && <h3 className="message-title">{entry.title}</h3>}
        <ActivityBody
          key={`${entry.key}-${entry.version}`}
          entry={entry}
          flowId={flowId}
        />
        {(Object.keys(entry.metadata).length > 0 ||
          entry.url ||
          (entry.previous_version !== null && !entry.deleted)) && (
          <div className="message-footer">
            {Object.keys(entry.metadata).length > 0 && (
              <SavedDetails
                className="message-provenance"
                stateKey={`${entry.key}-${entry.version}-metadata`}
              >
                <summary>来源信息与查询定位</summary>
                <TextWindow
                  code
                  text={JSON.stringify(entry.metadata, null, 2)}
                  stateKey={`${entry.key}-${entry.version}-metadata-text`}
                />
              </SavedDetails>
            )}
            {entry.url && (
              <a
                className="message-source-link"
                href={entry.url}
                target="_blank"
                rel="noreferrer"
              >
                在 GitHub 中查看 ↗
              </a>
            )}
            <EditedDiff
              key={`diff-${entry.key}-${entry.version}`}
              entry={entry}
              flowId={flowId}
            />
          </div>
        )}
        {entry.kind === "thread" && (
          <ThreadReplies
            key={`${flowId}-${entry.key}-${snapshot}`}
            entry={entry}
            flowId={flowId}
            snapshot={snapshot}
          />
        )}
      </div>
    </article>
  );
}
