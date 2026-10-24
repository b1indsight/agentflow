import {
  type KeyboardEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  Archive,
  ArrowLeft,
  ChevronDown,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
} from "lucide-react";
import {
  archiveFlow,
  getFlow,
  getFlows,
  getJobs,
  getRepos,
  retryFlow,
  retryFailureMessage,
} from "./api";
import {
  ActivityView,
  isOperationActivity,
  isToolActivity,
  sameActivityGroup,
  type ActivityGroup,
} from "./activity";
import { ConversationSources, SourceList } from "./ConversationSources";
import { actionLabel, phases } from "./phases";
import { SearchBox } from "./SearchBox";
import type {
  ActivityItem,
  FlowDetail,
  FlowSummary,
  JobSummary,
  Phase,
  Tab,
} from "./types";
import { useConversation } from "./useConversation";
import { SavedDetails, WindowList } from "./windowing";
import { focusableControls } from "./focus";

type Column = {
  items: FlowSummary[];
  next: string | null;
  more: boolean;
  busy: boolean;
  error: string;
};
const column = (): Column => ({
  items: [],
  next: null,
  more: true,
  busy: false,
  error: "",
});
const blank = (): Record<Phase, Column> => ({
  queued: column(),
  running: column(),
  review: column(),
  blocked: column(),
  done: column(),
});
const positions = new Map<string, number>();
const route = () => {
  const m = location.hash.match(
    /^#flow\/(\d+)(?:\/(overview|conversation|jobs))?$/,
  );
  return {
    id: m ? Number(m[1]) : null,
    tab: (m?.[2] ?? "conversation") as Tab,
  };
};
const name = (f: FlowSummary) => f.title ?? `Issue #${f.issue_number}`;
const ref = (f: FlowSummary) =>
  f.pr_number ? `PR #${f.pr_number}` : `Issue #${f.issue_number}`;

function Board({
  columns,
  counts,
  load,
  go,
}: {
  columns: ReturnType<typeof blank>;
  counts: Record<Phase, number>;
  load: (p: Phase) => void;
  go: (id: number) => void;
}) {
  const [mobile, setMobile] = useState<Phase>("running");
  const host = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const roots = [
      ...(host.current?.querySelectorAll<HTMLElement>("[data-phase]") ?? []),
    ];
    roots.forEach((x) => {
      x.scrollTop = positions.get(x.dataset.phase!) ?? 0;
    });
    const frame = requestAnimationFrame(() => {
      host.current
        ?.querySelector<HTMLElement>(
          `[data-flow="${positions.get("card-focus")}"]`,
        )
        ?.focus({ preventScroll: true });
    });
    return () => {
      cancelAnimationFrame(frame);
      roots.forEach((x) => positions.set(x.dataset.phase!, x.scrollTop));
    };
  }, []);
  return (
    <main ref={host} className="board-page" aria-label="工作看板">
      <div className="mobile-stages">
        {phases.map(([p, l]) => (
          <button
            key={p}
            className={`phase-${p}${mobile === p ? " active" : ""}`}
            onClick={() => setMobile(p)}
          >
            <i className="dot" />
            {l}
            <span>{counts[p]}</span>
          </button>
        ))}
      </div>
      <section className="kanban" aria-label="Flow 看板">
        {phases.map(([phase, label]) => {
          const c = columns[phase];
          return (
            <section
              className={`column phase-${phase}${mobile === phase ? " mobile-selected" : ""}`}
              key={phase}
            >
              <div className="col-header">
                <i className="dot" />
                {label}
                <span className="count">{counts[phase]}</span>
              </div>
              <div
                className="column-scroll"
                data-scroll-root
                data-phase={phase}
                onScroll={(e) => {
                  const x = e.currentTarget;
                  if (x.scrollTop + x.clientHeight >= x.scrollHeight - 120)
                    load(phase);
                }}
              >
                <WindowList
                  count={c.items.length}
                  label={`看板-${phase}`}
                  itemKey={(i) => c.items[i].id}
                  estimate={250}
                >
                  {(i) => {
                    const f = c.items[i];
                    return (
                      <button
                        className={`card phase-${f.phase}`}
                        data-flow={f.id}
                        onClick={() => {
                          positions.set("card-focus", f.id);
                          go(f.id);
                        }}
                      >
                        <div className="card-top">
                          <span>{ref(f)}</span>
                        </div>
                        <h2>{name(f)}</h2>
                        <div className="repo-label">{f.repo}</div>
                        <div className="card-foot">
                          <span className="current-action">
                            <i className="dot" />
                            {actionLabel(f)}
                          </span>
                        </div>
                      </button>
                    );
                  }}
                </WindowList>
                {c.error && (
                  <button className="btn" onClick={() => load(phase)}>
                    读取失败：{c.error}（重试）
                  </button>
                )}
                {c.busy && <p>正在加载…</p>}
                {!c.busy && c.more && <div className="load-sentinel" />}
                {!c.items.length && !c.busy && (
                  <div className="empty-col">暂无任务</div>
                )}
              </div>
            </section>
          );
        })}
      </section>
    </main>
  );
}

function Detail({
  id,
  tab,
  all,
  counts,
  load,
  refresh,
  onChanged,
  onBack,
}: {
  id: number;
  tab: Tab;
  all: FlowSummary[];
  counts: Record<Phase, number>;
  load: (p: Phase) => void;
  refresh: number;
  onChanged: () => void;
  onBack: () => void;
}) {
  const [flow, setFlow] = useState<FlowDetail | null>(null),
    [error, setError] = useState(""),
    [collapsed, setCollapsed] = useState(
      () => positions.get("collapsed") === 1,
    ),
    [nav, setNav] = useState(false);
  const [pending, setPending] = useState<"archive" | "retry" | null>(null);
  const writing = useRef(false);
  const [archiveError, setArchiveError] = useState("");
  const [retryError, setRetryError] = useState("");
  const [retryNotice, setRetryNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const submit = async (action: "archive" | "retry") => {
    // Fence both writes synchronously, before React renders disabled buttons.
    if (writing.current) return;
    writing.current = true;
    setPending(action);
    setArchiveError("");
    setRetryError("");
    setRetryNotice("");
    try {
      // Let the write finish after navigation so the board can refresh its committed outcome.
      const value = await (action === "retry"
        ? retryFlow(id)
        : archiveFlow(id));
      onChanged();
      if (mounted.current) {
        setFlow((prior) =>
          prior && prior.state_version > value.state_version ? prior : value,
        );
        if (action === "retry")
          setRetryNotice("重试请求已受理，请查看当前动作及执行记录。");
      }
    } catch (error) {
      if (mounted.current) {
        if (action === "retry") setRetryError(retryFailureMessage(error));
        else
          setArchiveError(error instanceof Error ? error.message : "请求失败");
      }
    } finally {
      writing.current = false;
      if (mounted.current) setPending(null);
    }
  };
  const [compactHead, setCompactHead] = useState(false);
  const head = useRef<HTMLElement>(null);
  const expandedHeight = useRef(0);
  const headTransition = useRef<{ height: number; scroll: number } | null>(
    null,
  );
  const expandedAt = useRef<number | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]),
    [jobNext, setJobNext] = useState<string | null>(null),
    [jobMore, setJobMore] = useState(true),
    [jobError, setJobError] = useState("");
  const detail = useRef<HTMLDivElement>(null),
    rail = useRef<HTMLElement>(null);
  const jobsRequest = useRef<AbortController | null>(null);
  const jobsBusy = useRef(false);
  useLayoutEffect(() => {
    const node = detail.current;
    if (!node) return;
    const key = `detail:${id}:${tab}`;
    node.scrollTop = positions.get(key) ?? 0;
    return () => {
      positions.set(key, node.scrollTop);
    };
  }, [id, tab, Boolean(flow)]);
  useLayoutEffect(() => {
    const node = detail.current;
    const header = head.current;
    const before = headTransition.current;
    if (node && header && before && before.scroll > before.height) {
      node.scrollTop = Math.max(
        0,
        before.scroll + header.offsetHeight - before.height,
      );
      if (expandedAt.current !== null) expandedAt.current = node.scrollTop;
    }
    if (
      compactHead &&
      header
        ?.querySelector(".detail-head-full")
        ?.contains(document.activeElement)
    ) {
      header
        .querySelector<HTMLButtonElement>(
          ".compact-detail-bar button:last-child",
        )
        ?.focus({ preventScroll: true });
    }
    headTransition.current = null;
  }, [compactHead]);
  useEffect(() => {
    const node = detail.current;
    if (!node) return;
    expandedAt.current = null;
    const mobile = matchMedia("(max-width: 760px)");
    const update = () => {
      const header = head.current;
      if (!header) return;
      if (!header.classList.contains("compact"))
        expandedHeight.current = header.offsetHeight;
      if (
        expandedAt.current !== null &&
        Math.abs(node.scrollTop - expandedAt.current) > 48
      )
        expandedAt.current = null;
      const shrink =
        mobile.matches &&
        expandedAt.current === null &&
        (header.classList.contains("compact")
          ? node.scrollTop > 8
          : node.scrollTop > expandedHeight.current + 96);
      if (shrink !== header.classList.contains("compact")) {
        headTransition.current = {
          height: header.offsetHeight,
          scroll: node.scrollTop,
        };
        setCompactHead(shrink);
      }
    };
    node.addEventListener("scroll", update, { passive: true });
    mobile.addEventListener("change", update);
    window.addEventListener("resize", update);
    update();
    return () => {
      node.removeEventListener("scroll", update);
      mobile.removeEventListener("change", update);
      window.removeEventListener("resize", update);
    };
  }, [id, tab, Boolean(flow)]);
  useEffect(() => {
    const c = new AbortController();
    setError("");
    void getFlow(id, c.signal)
      .then(
        (value) =>
          !c.signal.aborted &&
          setFlow((prior) =>
            prior && prior.state_version > value.state_version ? prior : value,
          ),
      )
      .catch((e) => !c.signal.aborted && setError(e.message));
    return () => c.abort();
  }, [id, refresh]);
  const moreJobs = (reset = false) => {
    if (!reset && (!jobMore || jobsBusy.current)) return;
    jobsRequest.current?.abort();
    const c = new AbortController();
    jobsRequest.current = c;
    jobsBusy.current = true;
    setJobError("");
    void getJobs(id, reset ? null : jobNext, c.signal)
      .then((p) => {
        if (c.signal.aborted) return;
        setJobs((x) => (reset ? p.items : [...x, ...p.items]));
        setJobNext(p.next_cursor);
        setJobMore(p.has_more);
      })
      .catch((e) => !c.signal.aborted && setJobError(e.message))
      .finally(() => {
        if (jobsRequest.current === c) jobsBusy.current = false;
      });
  };
  useEffect(() => {
    setJobs([]);
    setJobNext(null);
    setJobMore(true);
    setJobError("");
    if (tab === "jobs") moreJobs(true);
    return () => jobsRequest.current?.abort();
  }, [id, tab, refresh]);
  useEffect(() => {
    if (!nav) return;
    const before = document.activeElement;
    focusableControls(rail.current)[0]?.focus();
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setNav(false);
      if (e.key === "Tab") {
        const controls = focusableControls(rail.current),
          index = controls.findIndex((x) => x === document.activeElement);
        if (
          (e.shiftKey && index <= 0) ||
          (!e.shiftKey && index === controls.length - 1)
        ) {
          e.preventDefault();
          (e.shiftKey ? controls.at(-1) : controls[0])?.focus();
        }
      }
    };
    addEventListener("keydown", key);
    return () => {
      removeEventListener("keydown", key);
      if (before instanceof HTMLElement) before.focus();
    };
  }, [nav]);
  if (error)
    return (
      <main className="empty-state">
        <strong>无法读取 Flow</strong>
        <span>{error}</span>
        <button className="btn" onClick={onBack}>
          返回看板
        </button>
      </main>
    );
  if (!flow) return <main className="empty-state">正在读取 Flow…</main>;
  const switchTab = (t: Tab) => (location.hash = `flow/${id}/${t}`);
  const tabs = (e: KeyboardEvent) => {
    if (!["ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const x: Tab[] = ["overview", "conversation", "jobs"],
      n = (x.indexOf(tab) + (e.key === "ArrowRight" ? 1 : 2)) % 3;
    switchTab(x[n]);
  };
  return (
    <div
      className={`detail-shell${collapsed ? " rail-collapsed" : ""}${nav ? " nav-open" : ""}`}
    >
      <aside
        ref={rail}
        className="rail"
        aria-label="Flow 导航"
        role={nav ? "dialog" : undefined}
        aria-modal={nav || undefined}
      >
        <div className="rail-head">
          <button className="btn ghost back" onClick={onBack}>
            返回看板
          </button>
          <button
            className="btn rail-toggle"
            aria-label={collapsed ? "展开任务侧栏" : "折叠任务侧栏"}
            onClick={() =>
              setCollapsed((x) => {
                positions.set("collapsed", x ? 0 : 1);
                return !x;
              })
            }
          >
            {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
          </button>
        </div>
        <nav className="rail-scroll" data-scroll-root>
          {phases.map(([p, l]) => {
            const group = all.filter((f) => f.phase === p);
            return (
              <SavedDetails
                open
                key={p}
                stateKey={`rail-group-${p}`}
                className={`rail-group phase-${p}`}
              >
                <summary>
                  {l}
                  <span className="count">{counts[p]}</span>
                </summary>
                <WindowList
                  count={group.length}
                  label={`侧栏-${p}`}
                  itemKey={(i) => group[i].id}
                  estimate={78}
                >
                  {(i) => (
                    <button
                      className={`flow-tab${group[i].id === id ? " active" : ""}`}
                      onClick={() => {
                        setNav(false);
                        location.hash = `flow/${group[i].id}/${tab}`;
                      }}
                    >
                      <i className="dot" />
                      <span className="name">{name(group[i])}</span>
                    </button>
                  )}
                </WindowList>
                {group.length < counts[p] && (
                  <button className="btn" onClick={() => load(p)}>
                    更多{l}任务
                  </button>
                )}
              </SavedDetails>
            );
          })}
        </nav>
      </aside>
      <button
        className="mobile-shade"
        aria-label="关闭任务导航"
        onClick={() => setNav(false)}
      />
      <main className="detail">
        <header
          ref={head}
          className={`detail-head${compactHead ? " compact" : ""}`}
        >
          {compactHead && (
            <div className="compact-detail-bar">
              <button
                type="button"
                className="btn ghost"
                aria-label="返回看板"
                onClick={onBack}
              >
                <ArrowLeft />
              </button>
              <span className="compact-detail-ref">{ref(flow)}</span>
              <span className="compact-detail-title">{name(flow)}</span>
              <button
                type="button"
                className="btn ghost"
                aria-label="展开详情标题"
                onClick={() => {
                  expandedAt.current = detail.current?.scrollTop ?? 0;
                  headTransition.current = {
                    height: head.current?.offsetHeight ?? 0,
                    scroll: detail.current?.scrollTop ?? 0,
                  };
                  setCompactHead(false);
                }}
              >
                <ChevronDown />
              </button>
            </div>
          )}
          <div className="detail-head-full" hidden={compactHead}>
            <button
              className="btn mobile-nav"
              aria-expanded={nav}
              onClick={() => setNav(true)}
            >
              <Menu />
              切换任务
            </button>
            <div className="detail-title-row">
              <h1 id="detail-heading">{name(flow)}</h1>
              <span className={`badge phase-${flow.phase}`}>
                <i className="dot" />
                {actionLabel(flow)}
              </span>
            </div>
            <div className="links">
              <span>{flow.repo}</span>
              <a href={flow.issue_url} target="_blank" rel="noreferrer">
                Issue #{flow.issue_number} ↗
              </a>
              {flow.pr_url && (
                <a href={flow.pr_url} target="_blank" rel="noreferrer">
                  PR #{flow.pr_number} ↗
                </a>
              )}
            </div>
            <div className="tabs" role="tablist" onKeyDown={tabs}>
              {(
                [
                  ["overview", "概览"],
                  ["conversation", "会话"],
                  ["jobs", "执行记录"],
                ] as [Tab, string][]
              ).map(([t, l]) => (
                <button
                  role="tab"
                  id={`tab-${t}`}
                  aria-selected={tab === t}
                  tabIndex={tab === t ? 0 : -1}
                  onClick={() => switchTab(t)}
                  key={t}
                >
                  {l}
                </button>
              ))}
            </div>
          </div>
        </header>
        <div className="detail-body" data-scroll-root ref={detail}>
          <section className="content" role="tabpanel">
            {tab === "overview" ? (
              <>
                <section className={`execution-summary phase-${flow.phase}`}>
                  <span className="eyebrow">当前动作</span>
                  <h2>{actionLabel(flow)}</h2>
                  <p>{flow.reason}</p>
                  <div className="next-step">
                    <span>下一步条件</span>
                    <p>{flow.next_condition}</p>
                  </div>
                  {flow.phase === "blocked" && (
                    <button
                      className="btn"
                      disabled={pending !== null}
                      onClick={() => void submit("retry")}
                    >
                      <RefreshCw />
                      {pending === "retry" ? "正在提交重试…" : "重试"}
                    </button>
                  )}
                  {flow.lifecycle === "cancelled" && flow.phase !== "done" && (
                    <>
                      <p className="muted">
                        归档只调整展示分组，保留取消状态和全部历史。
                      </p>
                      <button
                        className="btn"
                        disabled={pending !== null}
                        onClick={() => void submit("archive")}
                      >
                        <Archive />
                        {pending === "archive" ? "正在归档…" : "归档"}
                      </button>
                    </>
                  )}
                  {archiveError && (
                    <p role="alert">归档失败：{archiveError}，请重试。</p>
                  )}
                  {retryError && <p role="alert">{retryError}</p>}
                  {retryNotice && <p role="status">{retryNotice}</p>}
                </section>
                <h2 className="panel-title">任务信息</h2>
                <dl className="task-facts">
                  <dt>生命周期 / 阶段</dt>
                  <dd>
                    {flow.lifecycle === "cancelled" ? "已取消" : flow.lifecycle}{" "}
                    / {phases.find(([p]) => p === flow.phase)?.[1]}
                  </dd>
                  <dt>Worker</dt>
                  <dd>
                    {flow.worker_id ?? "尚未绑定"}
                    {flow.worker_historical ? "（历史归属）" : ""}
                  </dd>
                  <dt>分支</dt>
                  <dd>{flow.branch ?? "尚未创建"}</dd>
                  <dt>修订轮数</dt>
                  <dd>{flow.revision_cycles}</dd>
                  <dt>创建</dt>
                  <dd>{new Date(flow.created_at).toLocaleString()}</dd>
                  <dt>更新</dt>
                  <dd>{new Date(flow.updated_at).toLocaleString()}</dd>
                  {flow.archived_at && (
                    <>
                      <dt>手动归档</dt>
                      <dd>{new Date(flow.archived_at).toLocaleString()}</dd>
                    </>
                  )}
                  <dt>当前状态版本</dt>
                  <dd>{flow.state_version}（与阅读快照独立）</dd>
                </dl>
                <h2 className="panel-title">数据来源</h2>
                <SourceList sources={flow.sources} />
              </>
            ) : tab === "conversation" ? (
              <Conversation key={id} id={id} root={detail} refresh={refresh} />
            ) : (
              <>
                <div className="timeline">
                  <WindowList
                    count={jobs.length}
                    label="执行记录"
                    itemKey={(i) => jobs[i].id}
                  >
                    {(i) => (
                      <article
                        className="timeline-item"
                        data-state={jobs[i].state}
                      >
                        <div className="job-top">
                          <strong>
                            {jobs[i].kind} #{jobs[i].attempt}
                          </strong>
                          <span className="badge">{jobs[i].state}</span>
                        </div>
                        <time dateTime={jobs[i].created_at}>
                          创建于 {new Date(jobs[i].created_at).toLocaleString()}
                        </time>
                        <p>
                          {jobs[i].worker_id ?? "无 Worker"}
                          {jobs[i].error_code ? ` · ${jobs[i].error_code}` : ""}
                        </p>
                      </article>
                    )}
                  </WindowList>
                </div>
                {jobError && (
                  <button className="btn" onClick={() => moreJobs()}>
                    读取失败：{jobError}（重试）
                  </button>
                )}
                {jobMore && (
                  <button className="btn" onClick={() => moreJobs()}>
                    读取更多执行记录
                  </button>
                )}
              </>
            )}
          </section>
        </div>
      </main>
    </div>
  );
}

function Conversation({
  id,
  root,
  refresh,
}: {
  id: number;
  root: React.RefObject<HTMLElement | null>;
  refresh: number;
}) {
  const { state, load, onRange, retry } = useConversation(id, root);
  const previousRefresh = useRef(refresh);
  useEffect(() => {
    if (previousRefresh.current !== refresh) {
      previousRefresh.current = refresh;
      load(true);
    }
  }, [refresh, load]);
  return (
    <>
      <ConversationSources
        sources={state.sources}
        busy={state.busy}
        refresh={() => load(true)}
      />
      {Object.values(state.changes).some(Boolean) && (
        <p className="conversation-divider" role="status">
          快照更新：
          {Object.entries(state.changes)
            .filter(([, n]) => n)
            .map(([k, n]) => `${k} ${n}`)
            .join("，")}
        </p>
      )}
      <div className="conversation-stream">
        <WindowList
          count={state.keys.length}
          label="会话活动"
          itemKey={(i) => state.keys[i] ?? `${id}-${i}`}
          onRange={onRange}
          estimate={220}
        >
          {(i, mounted) => {
            const e = state.items.get(i);
            const previous = mounted.has(i - 1)
              ? state.items.get(i - 1)
              : undefined;
            // Keep raw rows virtualized; only their compact activity bar and open state are shared.
            let activityGroup: ActivityGroup | undefined;
            const toolResults: ActivityItem[] = [];
            let mergedIntoCall = false;
            const tool = e?.source === "worker" && isToolActivity(e);
            if (tool || isOperationActivity(e)) {
              const belongs = (item: ActivityItem | undefined) =>
                (tool ? isToolActivity(item) : isOperationActivity(item)) &&
                sameActivityGroup(e, item);
              let start = i;
              while (belongs(state.items.get(start - 1))) start--;
              let first = i,
                end = i;
              while (mounted.has(first - 1) && first > start) first--;
              while (mounted.has(end + 1) && belongs(state.items.get(end + 1)))
                end++;
              let calls = 0,
                results = 0;
              const callIndexes = new Map<string, number>();
              for (let index = first; tool && index <= end; index++) {
                const item = state.items.get(index);
                if (!item) continue;
                const callId = item.metadata.call_id;
                if (item.kind === "tool_call") {
                  calls++;
                  if (typeof callId === "string" && callId)
                    callIndexes.set(callId, index);
                } else {
                  results++;
                  const callIndex =
                    typeof callId === "string" && callId
                      ? callIndexes.get(callId)
                      : undefined;
                  if (callIndex !== undefined) {
                    if (callIndex === i) toolResults.push(item);
                    if (index === i) mergedIntoCall = true;
                  }
                }
              }
              const partial =
                first > start || belongs(state.items.get(end + 1));
              if (tool || first < end || partial)
                activityGroup = {
                  key: state.keys[start],
                  leader: i === first,
                  summary: tool
                    ? `${calls} 次调用 · ${results} 条结果`
                    : `${end - first + 1} 条`,
                  partial,
                };
            }
            const provenance: ActivityItem[] = [];
            if (e && !sameActivityGroup(previous, e)) {
              provenance.push(e);
              for (let index = i + 1; mounted.has(index); index++) {
                const following = state.items.get(index);
                if (!following || !sameActivityGroup(e, following)) break;
                provenance.push(following);
              }
            }
            return e ? (
              <ActivityView
                key={`${e.key}-${e.version}`}
                entry={e}
                flowId={id}
                snapshot={state.snapshot ?? 0}
                continuation={sameActivityGroup(previous, e)}
                activityGroup={activityGroup}
                toolResults={toolResults}
                mergedIntoCall={mergedIntoCall}
                provenance={provenance}
              />
            ) : (
              <div className="history-placeholder">正在读取历史交互…</div>
            );
          }}
        </WindowList>
      </div>
      {state.error ? (
        <button className="btn" onClick={retry}>
          读取失败：{state.error.message}（重试）
        </button>
      ) : state.has_more ? (
        <div className="load-sentinel" role="status">
          {state.busy ? "正在加载更多交互…" : "向下滚动，继续读取交互"}
        </div>
      ) : (
        <button
          className="refresh-boundary conversation-divider"
          disabled={state.busy}
          onClick={() => void load(true)}
        >
          {state.busy ? "正在检查更新…" : "已到当前本地快照末尾 · 点击刷新"}
        </button>
      )}
    </>
  );
}

export default function App() {
  const [current, setCurrent] = useState(route),
    [query, setQuery] = useState(
      new URL(location.href).searchParams.get("q") ?? "",
    ),
    [repos, setRepos] = useState<string[]>([]),
    [repoNext, setRepoNext] = useState<string | null>(null),
    [repoError, setRepoError] = useState(""),
    [refresh, setRefresh] = useState(0),
    [boardRevision, setBoardRevision] = useState(0),
    [columns, setColumns] = useState(blank),
    [counts, setCounts] = useState(
      Object.fromEntries(phases.map(([p]) => [p, 0])) as Record<Phase, number>,
    );
  const boardRequests = useRef(new Map<Phase, AbortController>());
  useEffect(() => {
    const sync = () => setCurrent(route());
    addEventListener("hashchange", sync);
    return () => removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => {
    const c = new AbortController();
    void getRepos(null, c.signal)
      .then((page) => {
        if (!c.signal.aborted) {
          setRepos(page.items);
          setRepoNext(page.next_cursor);
        }
      })
      .catch((e) => !c.signal.aborted && setRepoError(e.message));
    return () => c.abort();
  }, []);
  const load = (phase: Phase, reset = false) => {
    const prior = columns[phase];
    if ((prior.busy || !prior.more) && !reset) return;
    boardRequests.current.get(phase)?.abort();
    const c = new AbortController();
    boardRequests.current.set(phase, c);
    setColumns((x) => ({
      ...x,
      [phase]: {
        ...(reset
          ? { items: [], next: null, more: true, error: "" }
          : x[phase]),
        busy: true,
        error: "",
      },
    }));
    void getFlows(
      {
        q: query,
        phase,
        cursor: reset ? undefined : (prior.next ?? undefined),
        limit: 50,
      },
      c.signal,
    )
      .then((page) => {
        if (c.signal.aborted || boardRequests.current.get(phase) !== c) return;
        setCounts(page.counts);
        setColumns((x) => ({
          ...x,
          [phase]: {
            items: reset ? page.items : [...x[phase].items, ...page.items],
            next: page.next_cursor,
            more: page.has_more,
            busy: false,
            error: "",
          },
        }));
      })
      .catch(
        (e) =>
          !c.signal.aborted &&
          boardRequests.current.get(phase) === c &&
          setColumns((x) => ({
            ...x,
            [phase]: { ...x[phase], busy: false, error: e.message },
          })),
      );
  };
  useEffect(() => {
    phases.forEach(([p]) => load(p, true));
    const url = new URL(location.href);
    if (query) url.searchParams.set("q", query);
    else url.searchParams.delete("q");
    history.replaceState(null, "", url);
    return () => boardRequests.current.forEach((c) => c.abort());
  }, [query, boardRevision]);
  const all = phases.flatMap(([p]) => columns[p].items);
  return (
    <>
      <header className="workspace-bar">
        <nav className="bar-path">
          <span>工作空间</span>
          <span>/</span>
          <strong>{current.id ? `任务详情` : "看板"}</strong>
        </nav>
        <div className="bar-filters">
          <SearchBox query={query} repos={repos} onQuery={setQuery} />
          {(repoNext || repoError) && (
            <button
              className="btn"
              onClick={() => {
                setRepoError("");
                void getRepos(repoNext)
                  .then((p) => {
                    setRepos((x) => (repoNext ? [...x, ...p.items] : p.items));
                    setRepoNext(p.next_cursor);
                  })
                  .catch((e) => setRepoError(e.message));
              }}
            >
              {repoError ? `仓库读取失败：${repoError}（重试）` : "更多仓库"}
            </button>
          )}
        </div>
        <div className="bar-status">
          <button
            className="btn ghost"
            aria-label="刷新当前视图"
            onClick={() => {
              if (current.id) setRefresh((x) => x + 1);
              else phases.forEach(([p]) => load(p, true));
            }}
          >
            <RefreshCw />
          </button>
        </div>
      </header>
      {current.id ? (
        <Detail
          key={current.id}
          id={current.id}
          tab={current.tab}
          all={all}
          counts={counts}
          load={load}
          refresh={refresh}
          onChanged={() => setBoardRevision((x) => x + 1)}
          onBack={() => (location.hash = "board")}
        />
      ) : (
        <Board
          columns={columns}
          counts={counts}
          load={load}
          go={(id) => (location.hash = `flow/${id}/conversation`)}
        />
      )}
    </>
  );
}
