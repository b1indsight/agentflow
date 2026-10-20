import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { Nodes, Root } from "mdast";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { getContent, getDiff, getThread } from "./api";
import { DiffWindow } from "./DiffWindow";
import type { ActivityItem, ActivityPage, ContentPage } from "./types";
import {
  CODE_CHARS,
  TEXT_CHARS,
  SavedDetails,
  TextWindow,
  textEnd,
  useReadingState,
} from "./windowing";

const markdownComponents: Components = {
  // Archived content must not make automatic requests to repository-supplied image URLs.
  img: ({ src, alt }) => (
    <a href={src} target="_blank" rel="noopener noreferrer">
      图片：{alt || "查看图片"}
    </a>
  ),
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
  table: ({ children }) => (
    <div className="markdown-table" tabIndex={0} aria-label="Markdown 表格">
      <table>{children}</table>
    </div>
  ),
};

/** Numeric identifiers keep labels such as `a-2` distinct from repeated citations of `a`. */
function numberFootnoteIdentifiers() {
  return (tree: Root) => {
    const identifiers = new Map<string, string>();
    const visit = (node: Nodes) => {
      if (
        node.type === "footnoteDefinition" ||
        node.type === "footnoteReference"
      ) {
        const key = node.identifier.toUpperCase();
        const identifier = identifiers.get(key) ?? String(identifiers.size + 1);
        identifiers.set(key, identifier);
        node.identifier = identifier;
      }
      if ("children" in node) node.children.forEach(visit);
    };
    visit(tree);
  };
}

/** Resolve archived destinations at their source; footnotes stay local to this body segment. */
function ActivityMarkdown({
  text,
  url: sourceUrl,
}: {
  text: string;
  url: string | null;
}) {
  const id = useId();
  const body = useRef<HTMLDivElement>(null);
  const footnoteLabel = `${id}-footnote-label`;
  // Focusing a row rerenders WindowList; keep anchors mounted so click and keyboard focus survive.
  const components = useMemo<Components>(
    () => ({
      ...markdownComponents,
      h2: ({ node: _node, id: headingId, ...props }) => (
        <h2
          {...props}
          id={headingId === "footnote-label" ? footnoteLabel : headingId}
        />
      ),
      a: ({ node, href, ...props }) => {
        const reference = node?.properties.dataFootnoteRef !== undefined;
        if (reference || node?.properties.dataFootnoteBackref !== undefined)
          return (
            <a
              {...props}
              href={href}
              aria-describedby={
                reference ? footnoteLabel : props["aria-describedby"]
              }
              onClick={(event) => {
                // Fragment navigation would overwrite the application's hash route.
                event.preventDefault();
                if (!href?.startsWith("#")) return;
                const target = document.getElementById(href.slice(1));
                if (target && body.current?.contains(target)) {
                  if (reference) target.tabIndex = -1;
                  target.focus({ preventScroll: true });
                  target.scrollIntoView({ block: "nearest" });
                }
              }}
              onAuxClick={(event) => event.preventDefault()}
            />
          );
        return (
          <a {...props} href={href} target="_blank" rel="noopener noreferrer" />
        );
      },
    }),
    [footnoteLabel],
  );
  return (
    <div ref={body} className="message-body markdown-body">
      <Markdown
        remarkPlugins={[remarkGfm, numberFootnoteIdentifiers]}
        remarkRehypeOptions={{ clobberPrefix: `${id}-` }}
        urlTransform={(url, _key, node) => {
          const safe = defaultUrlTransform(url);
          if (!safe) return undefined;
          if (
            node.properties.dataFootnoteRef !== undefined ||
            node.properties.dataFootnoteBackref !== undefined
          )
            return safe;
          if (/^[a-z][a-z\d+.-]*:/i.test(safe)) return safe;
          try {
            return (
              defaultUrlTransform(new URL(safe, sourceUrl ?? undefined).href) ||
              undefined
            );
          } catch {
            // Missing or malformed source URLs must not send relative links to the console.
            return undefined;
          }
        }}
        components={components}
      >
        {text}
      </Markdown>
    </div>
  );
}

/** Keep only one immutable body segment in memory; API offsets count Unicode code points. */
function ActivityBody({
  entry,
  flowId,
  framed = true,
}: {
  entry: ActivityItem;
  flowId: number;
  framed?: boolean;
}) {
  const tool = isToolActivity(entry);
  const budget = tool ? CODE_CHARS : TEXT_CHARS;
  const [savedStart, saveStart] = useReadingState(
    `${flowId}-${entry.key}-${entry.version}-body-offset`,
    0,
  );
  const initialStart = useRef(savedStart).current;
  const [segment, setSegment] = useState(() => ({
    text: entry.body.slice(0, textEnd(entry.body, 0, tool)),
    start: 0,
  }));
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const failedRead = useRef<{ offset: number; before?: number }>({
    offset: initialStart,
  });
  const read = async (offset: number, before?: number) => {
    if (request.current && !request.current.signal.aborted) return;
    const c = new AbortController();
    request.current = c;
    failedRead.current = { offset, before };
    setBusy(true);
    setError("");
    try {
      const p = await getContent(
        flowId,
        entry.key,
        entry.version,
        offset,
        before === undefined ? budget : before - offset,
        c.signal,
      );
      if (!c.signal.aborted) {
        // For backwards navigation, show the last bounded window before the current one.
        let start = 0;
        if (before !== undefined) {
          for (
            let end = textEnd(p.text, 0, tool);
            end < p.text.length;
            end = textEnd(p.text, start, tool)
          )
            start = end;
        }
        const nextStart = offset + Array.from(p.text.slice(0, start)).length;
        setSegment({
          text: p.text.slice(start, textEnd(p.text, start, tool)),
          start: nextStart,
        });
        saveStart(nextStart);
      }
    } catch (e) {
      if (!c.signal.aborted)
        setError(e instanceof Error ? e.message : "正文读取失败");
    } finally {
      if (request.current === c) request.current = null;
      if (!c.signal.aborted) setBusy(false);
    }
  };
  // Virtualized rows can unmount; restore only the selected immutable segment.
  useEffect(() => {
    if (initialStart > 0) void read(initialStart);
    return () => request.current?.abort();
  }, [initialStart]);
  const end = segment.start + Array.from(segment.text).length;
  const body = tool ? (
    <pre tabIndex={0} aria-label="工具摘要">
      <code>
        {!framed && entry.kind === "tool_call" && (
          <span className="tool-call-name">{entry.title || "调用"} </span>
        )}
        <span>{segment.text}</span>
      </code>
    </pre>
  ) : (
    <ActivityMarkdown text={segment.text} url={entry.url} />
  );
  const content = (
    <>
      {body}
      {(segment.start > 0 || end < entry.body_length) && (
        <div
          className="text-window-controls"
          role="group"
          aria-label="正文分段浏览"
          aria-busy={busy}
        >
          <button
            type="button"
            disabled={busy || segment.start === 0}
            onClick={() =>
              void read(Math.max(0, segment.start - budget), segment.start)
            }
          >
            上一段
          </button>
          <span>
            字符 {segment.start + 1}–{end} / {entry.body_length}
          </span>
          <button
            type="button"
            disabled={busy || end >= entry.body_length}
            onClick={() => void read(end)}
          >
            下一段
          </button>
        </div>
      )}
      {error && (
        <p role="alert">
          正文读取失败：{error}{" "}
          <button
            type="button"
            className="btn ghost"
            disabled={busy}
            onClick={() =>
              void read(failedRead.current.offset, failedRead.current.before)
            }
          >
            重试正文读取
          </button>
        </p>
      )}
    </>
  );
  return tool && framed ? (
    <SavedDetails
      className="tool"
      stateKey={`${entry.key}-${entry.version}-tool`}
    >
      <summary>
        ⌘{" "}
        {entry.title === "tool_result" ? "工具结果" : entry.title || "工具调用"}
      </summary>
      {content}
    </SavedDetails>
  ) : (
    content
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

/** Adjacent authors share identity; worker records must also have the same explicit session. */
export function sameActivityGroup(
  left: ActivityItem | undefined,
  right: ActivityItem | undefined,
): boolean {
  return Boolean(
    left &&
    right &&
    left.source === right.source &&
    !left.deleted &&
    !right.deleted &&
    left.prior === right.prior &&
    left.parent_key === right.parent_key &&
    [left, right].every(
      (item) =>
        item.kind !== "event" &&
        item.kind !== "thread" &&
        item.kind !== "check",
    ) &&
    ((left.author !== "" && left.author === right.author) ||
      (left.source === "worker" &&
        [left.author, right.author].every(
          (role) => role === "assistant" || role === "tool",
        ))) &&
    (left.source === "worker"
      ? ["attempt_id", "session_id", "session_generation"].every(
          (key) =>
            left.metadata[key] !== undefined &&
            left.metadata[key] !== null &&
            left.metadata[key] === right.metadata[key],
        )
      : left.kind === right.kind),
  );
}

function eventSummary(entry: ActivityItem): string {
  const event =
    typeof entry.metadata.event === "string"
      ? entry.metadata.event
      : entry.title;
  const actions: Record<string, string> = {
    labeled: "添加标签",
    unlabeled: "移除标签",
    assigned: "指派负责人",
    unassigned: "移除负责人",
    closed: "关闭",
    reopened: "重新打开",
    merged: "合并",
    locked: "锁定讨论",
    unlocked: "解锁讨论",
    milestoned: "设置里程碑",
    demilestoned: "移除里程碑",
    review_requested: "请求审核",
    review_request_removed: "取消审核请求",
  };
  const detail =
    event === "labeled" || event === "unlabeled"
      ? entry.metadata.label
      : event === "assigned" || event === "unassigned"
        ? entry.metadata.assignee
        : event === "milestoned" || event === "demilestoned"
          ? entry.metadata.milestone
          : event === "review_requested" || event === "review_request_removed"
            ? entry.metadata.requested_reviewer || entry.metadata.requested_team
            : null;
  return `${actions[event] || entry.title || event}${typeof detail === "string" && detail ? `：${detail}` : ""}`;
}

/** Tool calls and results share the same compact disclosure and text budget. */
export function isToolActivity(entry: ActivityItem | undefined): boolean {
  return entry?.kind === "tool_call" || entry?.kind === "tool_result";
}

/** A bounded mounted slice shares one disclosure without merging archive records or DOM rows. */
export interface ToolActivityGroup {
  key: string;
  leader: boolean;
  calls: number;
  results: number;
  partial: boolean;
}

/** Markdown produces safe React elements; repository-supplied HTML stays escaped text. */
export function ActivityView({
  entry,
  flowId = 0,
  snapshot = entry.version,
  continuation = false,
  toolGroup,
  toolResults = [],
  mergedIntoCall = false,
  provenance,
}: {
  entry: ActivityItem;
  flowId?: number;
  snapshot?: number;
  continuation?: boolean;
  toolGroup?: ToolActivityGroup;
  toolResults?: ActivityItem[];
  mergedIntoCall?: boolean;
  provenance?: ActivityItem[];
}) {
  const event = entry.kind === "event" && entry.body_length === 0;
  const tool = isToolActivity(entry);
  const [toolsOpen, setToolsOpen] = useReadingState(
    `${flowId}-${toolGroup?.key ?? entry.key}-tools`,
    0,
  );
  const title =
    event || tool || (entry.source === "worker" && entry.title === "message")
      ? ""
      : entry.title;
  const metadata =
    !continuation &&
    (Object.keys(entry.metadata).length > 0 || (provenance?.length ?? 0) > 1);
  return (
    <article
      className={`message${entry.deleted ? " deleted" : ""}${event ? " compact-event" : ""}${continuation ? " continuation" : ""}${toolGroup ? " tool-activity" : ""}`}
      data-entry={entry.key}
      hidden={
        mergedIntoCall || Boolean(toolGroup && !toolGroup.leader && !toolsOpen)
      }
      aria-label={
        continuation
          ? `${entry.author} · ${new Date(entry.occurred_at).toLocaleString()}`
          : undefined
      }
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
          {event && <span className="event-action">{eventSummary(entry)}</span>}
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
        {title && <h3 className="message-title">{title}</h3>}
        {toolGroup?.leader && (
          <button
            type="button"
            className="tool-activity-toggle"
            aria-expanded={Boolean(toolsOpen)}
            onClick={() => setToolsOpen(toolsOpen ? 0 : 1)}
          >
            <span aria-hidden="true">{toolsOpen ? "▾" : "▸"}</span> 工具活动
            {toolGroup.partial ? "（当前片段）" : ""} · {toolGroup.calls} 次调用
            · {toolGroup.results} 条结果
          </button>
        )}
        {toolGroup ? (
          !mergedIntoCall &&
          Boolean(toolsOpen) && (
            <section className="tool-activity-body" aria-label="工具活动详情">
              {entry.kind === "tool_result" ? (
                <blockquote
                  className="tool-activity-result"
                  aria-label="未配对工具结果"
                >
                  <ActivityBody entry={entry} flowId={flowId} framed={false} />
                </blockquote>
              ) : (
                <ActivityBody entry={entry} flowId={flowId} framed={false} />
              )}
              {toolResults.map((result) => (
                <blockquote
                  className="tool-activity-result"
                  data-entry={result.key}
                  key={`${result.key}-${result.version}`}
                >
                  <ActivityBody entry={result} flowId={flowId} framed={false} />
                  <EditedDiff entry={result} flowId={flowId} />
                </blockquote>
              ))}
            </section>
          )
        ) : entry.body_length > 0 || tool ? (
          <ActivityBody
            key={`${entry.key}-${entry.version}`}
            entry={entry}
            flowId={flowId}
          />
        ) : null}
        {!mergedIntoCall &&
          (!toolGroup || Boolean(toolsOpen)) &&
          (metadata ||
            entry.url ||
            (entry.previous_version !== null && !entry.deleted)) && (
            <div className="message-footer">
              {metadata && (
                <SavedDetails
                  className="message-provenance"
                  stateKey={`${entry.key}-${entry.version}-metadata`}
                >
                  <summary>
                    来源信息与查询定位
                    {provenance && provenance.length > 1
                      ? ` · ${provenance.length} 条记录`
                      : ""}
                  </summary>
                  <TextWindow
                    code
                    text={JSON.stringify(
                      provenance && provenance.length > 1
                        ? provenance.map((item) => ({
                            key: item.key,
                            version: item.version,
                            author: item.author,
                            occurred_at: item.occurred_at,
                            metadata: item.metadata,
                          }))
                        : entry.metadata,
                      null,
                      2,
                    )}
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
