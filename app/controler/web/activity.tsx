import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Info } from "lucide-react";
import type { Nodes, Root } from "mdast";
import Markdown, { defaultUrlTransform, type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { getContent, getDiff, getThread } from "./api";
import { DiffWindow } from "./DiffWindow";
import type {
  ActivityIdentity,
  ActivityItem,
  ActivityPage,
  ActivityProvenancePage,
  ActivityProvenanceRange,
  ActivityProvenanceSource,
  ContentPage,
  ConversationGroup,
  ConversationRow,
} from "./types";
import { ACTIVITY_PROVENANCE_ROWS } from "./useConversation";
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
  left: ActivityIdentity | undefined,
  right: ActivityIdentity | undefined,
): boolean {
  const operations = isOperationActivity(left) && isOperationActivity(right);
  const worker = left?.source === "worker" || right?.source === "worker";
  return Boolean(
    left &&
    right &&
    ((!worker && operations) || left.source === right.source) &&
    !left.deleted &&
    !right.deleted &&
    left.prior === right.prior &&
    left.parent_key === right.parent_key &&
    (operations ||
      [left, right].every(
        (item) =>
          item.kind !== "event" &&
          item.kind !== "thread" &&
          item.kind !== "check",
      )) &&
    ((left.author !== "" && left.author === right.author) ||
      (left.source === "worker" &&
        [left.author, right.author].every(
          (role) => role === "assistant" || role === "tool",
        ))) &&
    (worker
      ? (["attempt_id", "session_id", "session_generation"] as const).every(
          (key) =>
            left.metadata[key] !== undefined &&
            left.metadata[key] !== null &&
            left.metadata[key] === right.metadata[key],
        )
      : operations || left.kind === right.kind),
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
export function isToolActivity(entry: ActivityIdentity | undefined): boolean {
  return entry?.kind === "tool_call" || entry?.kind === "tool_result";
}

/** Checks and bodyless timeline events can share a compact actor disclosure. */
export function isOperationActivity(
  entry: ActivityIdentity | undefined,
): boolean {
  return (
    entry?.kind === "check" ||
    (entry?.kind === "event" && entry.body_length === 0)
  );
}

/** Keep display-key formatting here; opaque record keys may themselves contain colons. */
export function conversationRowKey(
  kind: ConversationRow["kind"],
  recordKey: string,
) {
  return `${kind}:${recordKey}`;
}

/** Recover the record key from a key produced by conversationRowKey. */
export function conversationRecordKey(rowKey: string) {
  return rowKey.slice(rowKey.indexOf(":") + 1);
}

/**
 * Project data-defined groups into independently virtualizable rows. Folded groups contain only a
 * summary; expanded tool results follow their nearest preceding matching call, one body per row.
 * Expanded keys identify read members, so refreshing a group's first record cannot close it.
 */
export function buildConversationRows(
  records: readonly ActivityIdentity[],
  expanded: ReadonlySet<string>,
): ConversationRow[] {
  const rows: ConversationRow[] = [];
  const body = (
    rawIndex: number,
    groupKey: string | null,
    pairedCallKey: string | null = null,
  ) => {
    rows.push({
      kind: "body",
      key: conversationRowKey("body", records[rawIndex].key),
      rawIndex,
      groupKey,
      pairedCallKey,
    });
  };
  for (let first = 0; first < records.length;) {
    const entry = records[first];
    const kind =
      entry.source === "worker" && isToolActivity(entry)
        ? "tool"
        : isOperationActivity(entry)
          ? "operation"
          : null;
    let last = first;
    if (kind)
      while (
        last + 1 < records.length &&
        (kind === "tool"
          ? isToolActivity(records[last + 1])
          : isOperationActivity(records[last + 1])) &&
        sameActivityGroup(entry, records[last + 1])
      )
        last++;
    if (kind === null || (kind === "operation" && first === last)) {
      body(first, null);
      first++;
      continue;
    }
    const group: ConversationGroup = {
      key: entry.key,
      kind,
      first,
      last,
      calls: 0,
      results: 0,
    };
    const calls = new Map<string, number>();
    const results = new Map<number, number[]>();
    const paired = new Map<number, number>();
    let open = false;
    for (let index = first; index <= last; index++) {
      const item = records[index];
      if (expanded.has(item.key)) open = true;
      if (kind !== "tool") continue;
      const callId = item.metadata.call_id;
      if (item.kind === "tool_call") {
        group.calls++;
        if (typeof callId === "string" && callId) calls.set(callId, index);
      } else {
        group.results++;
        const call =
          typeof callId === "string" && callId ? calls.get(callId) : undefined;
        if (call !== undefined) {
          paired.set(index, call);
          const matches = results.get(call) ?? [];
          matches.push(index);
          results.set(call, matches);
        }
      }
    }
    rows.push({
      kind: "summary",
      key: conversationRowKey("summary", group.key),
      group,
      expanded: open,
    });
    if (open)
      for (let index = first; index <= last; index++) {
        if (paired.has(index)) continue;
        body(index, group.key);
        for (const result of results.get(index) ?? [])
          body(result, group.key, records[index].key);
      }
    first = last + 1;
  }
  return rows;
}

/** Return a merged source span only at its first raw record; body eviction cannot change its count. */
export function conversationProvenanceRange(
  records: readonly ActivityIdentity[],
  rawIndex: number,
): ActivityProvenanceRange | null {
  const entry = records[rawIndex];
  if (!entry || sameActivityGroup(records[rawIndex - 1], entry)) return null;
  let last = rawIndex;
  while (
    last + 1 < records.length &&
    sameActivityGroup(entry, records[last + 1])
  )
    last++;
  return last > rawIndex ? { first: rawIndex, last } : null;
}

/** A controlled, identity-only disclosure; expansion belongs to the conversation's local Set. */
export interface ActivitySummaryProps {
  entry: ActivityIdentity;
  group: ConversationGroup;
  expanded: boolean;
  onToggle: () => void;
  continuation?: boolean;
}

export function ActivitySummary({
  entry,
  group,
  expanded,
  onToggle,
  continuation = false,
}: ActivitySummaryProps) {
  return (
    <article
      className={`message grouped-activity${group.kind === "operation" ? " compact-event" : ""}${continuation ? " continuation" : ""}${entry.deleted ? " deleted" : ""}`}
      data-activity-summary={group.key}
      data-activity-group={group.key}
    >
      <div className="message-avatar" aria-hidden="true">
        {entry.author.slice(0, 1).toUpperCase()}
      </div>
      <div className="message-main">
        {/* Keep these identity labels aligned with standalone ActivityView records. */}
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
        <button
          type="button"
          className="activity-toggle"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          <span aria-hidden="true">{expanded ? "▾" : "▸"}</span>{" "}
          {group.kind === "tool"
            ? `工具活动 · ${group.calls} 次调用 · ${group.results} 条结果`
            : `操作记录 · ${group.last - group.first + 1} 条`}
        </button>
      </div>
    </article>
  );
}

/** Retain only the selected metadata page, separately from persisted panel/text reading state. */
function ProvenancePage({
  source,
  stateKey,
}: {
  source: ActivityProvenanceSource;
  stateKey: string;
}) {
  const [savedOffset, saveOffset] = useReadingState(`${stateKey}-page`, 0);
  const selected = useRef(savedOffset);
  const failedOffset = useRef(savedOffset);
  const [page, setPage] = useState<ActivityProvenancePage | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const read = async (offset: number) => {
    request.current?.abort();
    const c = new AbortController();
    request.current = c;
    failedOffset.current = offset;
    setBusy(true);
    setError("");
    try {
      const result = await source.read(source, offset, c.signal);
      if (!c.signal.aborted) {
        setPage(result);
        selected.current = result.offset;
        saveOffset(result.offset);
      }
    } catch (e) {
      if (!c.signal.aborted)
        setError(e instanceof Error ? e.message : "来源信息读取失败");
    } finally {
      if (!c.signal.aborted) setBusy(false);
    }
  };
  useEffect(() => {
    const lastPage =
      Math.floor((source.last - source.first) / ACTIVITY_PROVENANCE_ROWS) *
      ACTIVITY_PROVENANCE_ROWS;
    void read(Math.min(selected.current, lastPage));
    return () => request.current?.abort();
  }, [source.first, source.last, source.read]);
  return (
    <div aria-busy={busy}>
      {busy && !page && <p role="status">正在读取来源信息…</p>}
      {page && (
        <>
          <TextWindow
            code
            text={JSON.stringify(page.items, null, 2)}
            stateKey={`${stateKey}-${page.offset}-text`}
          />
          {page.total > ACTIVITY_PROVENANCE_ROWS && (
            <div
              className="text-window-controls"
              role="group"
              aria-label="来源记录分页"
            >
              <button
                type="button"
                disabled={busy || page.offset === 0}
                onClick={() =>
                  void read(Math.max(0, page.offset - ACTIVITY_PROVENANCE_ROWS))
                }
              >
                上一页来源记录
              </button>
              <span>
                记录 {page.offset + 1}–{page.offset + page.items.length} /{" "}
                {page.total}
              </span>
              <button
                type="button"
                disabled={busy || page.next_offset === null}
                onClick={() =>
                  page.next_offset !== null && void read(page.next_offset)
                }
              >
                下一页来源记录
              </button>
            </div>
          )}
        </>
      )}
      {error && (
        <p role="alert">
          来源信息读取失败：{error}{" "}
          <button
            type="button"
            disabled={busy}
            onClick={() => void read(failedOffset.current)}
          >
            重试来源信息
          </button>
        </p>
      )}
    </div>
  );
}

/** Each view owns one raw record only; group disclosure and result ordering are separate rows. */
export interface ActivityViewProps {
  entry: ActivityItem;
  flowId?: number;
  snapshot?: number;
  continuation?: boolean;
  groupKey?: string | null;
  pairedCallKey?: string | null;
  provenance?: ActivityProvenanceSource;
}

/** Markdown produces safe React elements; repository-supplied HTML stays escaped text. */
export function ActivityView({
  entry,
  flowId = 0,
  snapshot = entry.version,
  continuation = false,
  groupKey = null,
  pairedCallKey = null,
  provenance,
}: ActivityViewProps) {
  const event = entry.kind === "event" && entry.body_length === 0;
  const compact = isOperationActivity(entry);
  const tool = isToolActivity(entry);
  const grouped = groupKey !== null;
  const continuing = continuation || grouped;
  const [metadataOpen, setMetadataOpen] = useReadingState(
    `${entry.key}-${entry.version}-metadata`,
    0,
  );
  const metadataId = useId();
  const title =
    compact || tool || (entry.source === "worker" && entry.title === "message")
      ? ""
      : entry.title;
  let action = event ? eventSummary(entry) : entry.title;
  if (entry.kind === "check") {
    const workflow = entry.metadata.workflow_name;
    const conclusion = entry.metadata.conclusion;
    const status =
      typeof conclusion === "string" && conclusion
        ? conclusion
        : entry.metadata.status;
    action = `${typeof workflow === "string" && workflow ? workflow : "未知工作流"}: ${entry.title}: ${typeof status === "string" && status ? status : "unknown"}`;
  }
  const provenanceCount = provenance
    ? provenance.last - provenance.first + 1
    : 0;
  const metadata =
    (!continuation || compact) &&
    (Object.keys(entry.metadata).length > 0 || provenanceCount > 1);
  const provenanceLabel =
    "来源信息与查询定位" +
    (provenanceCount > 1 ? ` · ${provenanceCount} 条记录` : "");
  return (
    <article
      className={`message${entry.deleted ? " deleted" : ""}${compact ? " compact-event" : ""}${continuing ? " continuation" : ""}${grouped ? " grouped-activity" : ""}`}
      data-entry={entry.key}
      data-activity-group={groupKey ?? undefined}
      aria-label={
        continuing
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
          {compact && !grouped && (
            <span className="event-action">{action}</span>
          )}
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
        {compact && grouped && (
          <div className="operation-action">
            <span className="event-action">{action}</span>
            <time>{new Date(entry.occurred_at).toLocaleString()}</time>
          </div>
        )}
        {tool && grouped ? (
          <section className="tool-activity-body" aria-label="工具活动详情">
            {entry.kind === "tool_result" ? (
              <blockquote
                className="tool-activity-result"
                aria-label={
                  pairedCallKey === null ? "未配对工具结果" : "工具结果"
                }
                data-paired-call={pairedCallKey ?? undefined}
              >
                <ActivityBody
                  key={`${entry.key}-${entry.version}`}
                  entry={entry}
                  flowId={flowId}
                  framed={false}
                />
              </blockquote>
            ) : (
              <ActivityBody
                key={`${entry.key}-${entry.version}`}
                entry={entry}
                flowId={flowId}
                framed={false}
              />
            )}
          </section>
        ) : entry.body_length > 0 || tool ? (
          <ActivityBody
            key={`${entry.key}-${entry.version}`}
            entry={entry}
            flowId={flowId}
          />
        ) : null}
        {(metadata ||
          entry.url ||
          (entry.previous_version !== null && !entry.deleted)) && (
          <div className="message-footer">
            <EditedDiff
              key={`diff-${entry.key}-${entry.version}`}
              entry={entry}
              flowId={flowId}
            />
            {(entry.url || metadata) && (
              <div className="message-actions">
                {entry.url && (
                  <a
                    className="message-icon"
                    href={entry.url}
                    target="_blank"
                    rel="noreferrer"
                    aria-label="在 GitHub 中查看"
                    title="在 GitHub 中查看"
                  >
                    {/* Primer Octicons mark-github-16, padded to match the Info glyph. */}
                    <svg
                      className="github-mark"
                      viewBox="-0.75 -1 17.5 17.5"
                      aria-hidden="true"
                    >
                      <path d="M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656" />
                    </svg>
                  </a>
                )}
                {metadata && (
                  <button
                    type="button"
                    className="message-icon"
                    aria-label={provenanceLabel}
                    title={provenanceLabel}
                    aria-expanded={Boolean(metadataOpen)}
                    aria-controls={metadataId}
                    onClick={() => setMetadataOpen(metadataOpen ? 0 : 1)}
                  >
                    <Info aria-hidden="true" />
                  </button>
                )}
              </div>
            )}
            {metadata && (
              <div
                className="message-provenance"
                id={metadataId}
                hidden={!metadataOpen}
              >
                {provenance && provenanceCount > 1 ? (
                  Boolean(metadataOpen) && (
                    <ProvenancePage
                      key={snapshot}
                      source={provenance}
                      stateKey={`${entry.key}-${entry.version}-metadata`}
                    />
                  )
                ) : (
                  <TextWindow
                    code
                    text={JSON.stringify(entry.metadata, null, 2)}
                    stateKey={`${entry.key}-${entry.version}-metadata-text`}
                  />
                )}
              </div>
            )}
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
