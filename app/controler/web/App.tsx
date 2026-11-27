import {
  lazy,
  Suspense,
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import {
  Archive,
  ArrowDown,
  ArrowDownToLine,
  ArrowLeft,
  ChevronDown,
  Inbox,
  LoaderCircle,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  Settings,
} from "lucide-react";
import {
  appendAgentTask,
  archiveFlow,
  getFlow,
  getFlows,
  getJobs,
  getRepos,
  resumeFlow,
  retryFlow,
  retryFailureMessage,
  roundFailureMessage,
} from "./api";
import {
  ActivityMarkdown,
  ActivitySummary,
  ActivityView,
  buildConversationRows,
  conversationProvenanceRange,
  conversationRecordKey,
  conversationRowKey,
  OpenWorkerFile,
  sameActivityGroup,
} from "./activity";
import { ConversationSources, SourceList } from "./ConversationSources";
import { CreateFlow } from "./CreateFlow";
import { AgentTaskInput } from "./AgentTaskInput";
import { FileTab, FileTabRow } from "./FileTab";
import type { WorkerFile } from "./files";
import { InferenceNotices } from "./InferenceNotices";
import { actionLabel, archiveReasonLabels, flowLabel, phases } from "./phases";
import { SearchBox } from "./SearchBox";
import { matchesSearch, parseSearch } from "./search";
import { SettingsPage } from "./SettingsPage";
import { Button } from "./components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "./components/ui/tabs";
import type { FlowDetail, FlowSummary, JobSummary, Phase } from "./types";
import { useConversation } from "./useConversation";
import { SavedDetails, WindowList } from "./windowing";
import { focusableControls } from "./focus";

const FileViewer = lazy(() => import("./FileViewer"));

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
  archived: column(),
});
const sidebarGroups: {
  key: string;
  label: string;
  members: (readonly [Phase, string])[];
}[] = [
  {
    key: "inbox",
    label: "Inbox",
    members: phases.filter(([p]) => p === "blocked" || p === "review"),
  },
  ...phases.map((phase) => ({
    key: phase[0],
    label: phase[1],
    members: [phase],
  })),
];
const flowTabs = [
  { value: "overview", label: "概览" },
  { value: "conversation", label: "会话" },
  { value: "jobs", label: "执行记录" },
] as const;
type Tab = (typeof flowTabs)[number]["value"] | `file-${string}`;
// Column zero uses the Flow URL for selection; side columns select independently.
type FilePane = { files: WorkerFile[]; active: string | null };
const positions = new Map<string, number>();
const route = () => {
  const m = location.hash.match(/^#flow\/(\d+)(?:\/([^/]+))?$/);
  const base = flowTabs.find(
    ({ value }) => value === (m?.[2] ?? "conversation"),
  );
  const fileId = m?.[2]?.match(/^file-([0-9a-f]{32})$/)?.[1];
  const tab: Tab = fileId ? `file-${fileId}` : (base?.value ?? "conversation");
  return {
    settings: location.hash === "#settings",
    id: m && (base || fileId) ? Number(m[1]) : null,
    tab,
  };
};
function openFlow(id: number) {
  location.hash = `flow/${id}/conversation`;
}
const name = (f: FlowSummary) => f.title ?? `Flow #${f.id}`;
const ref = (f: FlowSummary) =>
  f.pr_number
    ? `PR #${f.pr_number}`
    : f.issue_number !== null
      ? `Issue #${f.issue_number}`
      : `Flow #${f.id}`;

function Board({
  columns,
  counts,
  load,
  go,
  mobile,
  onMobile,
}: {
  columns: ReturnType<typeof blank>;
  counts: Record<Phase, number>;
  load: (p: Phase) => void;
  go: (id: number) => void;
  mobile: Phase;
  onMobile: (phase: Phase) => void;
}) {
  const host = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const frame = requestAnimationFrame(() => {
      host.current
        ?.querySelector<HTMLElement>(
          `[data-flow="${positions.get("card-focus")}"]`,
        )
        ?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, []);
  return (
    <main ref={host} className="board-page" aria-label="工作看板">
      <div className="mobile-stages">
        {phases.map(([p, l]) => (
          <button
            key={p}
            className={`phase-${p}${mobile === p ? " active" : ""}`}
            onClick={() => onMobile(p)}
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
                  reading={{ key: `board:${phase}`, resolve: () => null }}
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
                        <div className="card-title">
                          <span
                            className="dot card-status"
                            role="img"
                            aria-label={flowLabel(f)}
                            title={flowLabel(f)}
                          />
                          <h2>{name(f)}</h2>
                        </div>
                        <div className="card-meta">
                          <span className="repo-label" title={f.repo}>
                            {f.repo}
                          </span>
                          <span className="card-ref">{ref(f)}</span>
                        </div>
                      </button>
                    );
                  }}
                </WindowList>
                {c.error && (
                  <button className="btn" onClick={() => load(phase)}>
                    <RefreshCw aria-hidden="true" />
                    <span>读取失败：{c.error}（重试）</span>
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
  summary,
  tab,
  columns,
  counts,
  load,
  refresh,
  onChanged,
  onBack,
}: {
  id: number;
  summary?: FlowSummary;
  tab: Tab;
  columns: Record<Phase, Column>;
  counts: Record<Phase, number>;
  load: (p: Phase) => void;
  refresh: number;
  onChanged: (flow: FlowDetail) => void;
  onBack: () => void;
}) {
  const [collapsed, setCollapsed] = useState(
      () => positions.get("collapsed") === 1,
    ),
    [nav, setNav] = useState(false);
  const rail = useRef<HTMLElement>(null);
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
          <button
            className="btn ghost back"
            aria-label="返回看板"
            title="返回看板"
            onClick={onBack}
          >
            <ArrowLeft aria-hidden="true" />
          </button>
          <button
            className="btn rail-toggle"
            aria-label={collapsed ? "展开任务侧栏" : "折叠任务侧栏"}
            title={collapsed ? "展开任务侧栏" : "折叠任务侧栏"}
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
          {sidebarGroups.map(({ key, label, members }) => {
            const group = members.flatMap(([p]) => columns[p].items);
            const inbox = key === "inbox";
            return (
              <SavedDetails
                open
                key={key}
                stateKey={`rail-group-${key}`}
                className={`rail-group${inbox ? " rail-inbox" : ` phase-${key}`}`}
              >
                <summary>
                  {inbox && <Inbox aria-hidden="true" />}
                  {label}
                  <span className="count">
                    {members.reduce((total, [p]) => total + counts[p], 0)}
                  </span>
                </summary>
                <WindowList
                  count={group.length}
                  label={`侧栏-${key}`}
                  itemKey={(i) => group[i].id}
                  estimate={78}
                >
                  {(i) => (
                    <button
                      className={`flow-tab phase-${group[i].phase}${group[i].id === id ? " active" : ""}`}
                      aria-current={group[i].id === id ? "page" : undefined}
                      onClick={() => {
                        setNav(false);
                        location.hash = `flow/${group[i].id}/${tab}`;
                      }}
                    >
                      <i className="dot" />
                      <span className="name">
                        {name(group[i])}
                        {inbox && (
                          <span className="meta">
                            {group[i].repo} · {ref(group[i])}
                          </span>
                        )}
                      </span>
                    </button>
                  )}
                </WindowList>
                {inbox &&
                  !group.length &&
                  members.every(
                    ([p]) =>
                      !columns[p].busy && !columns[p].error && !columns[p].more,
                  ) && (
                    <p className="inbox-note">当前筛选下暂无待人工处理的任务</p>
                  )}
                {members.map(([p, phaseLabel]) => {
                  const c = columns[p];
                  const label = c.error
                    ? `重试读取${phaseLabel}任务`
                    : `更多${phaseLabel}任务`;
                  return (
                    <div key={p}>
                      {c.busy && (
                        <p className="inbox-note" role="status">
                          正在加载{phaseLabel}任务…
                        </p>
                      )}
                      {c.error && (
                        <p className="inbox-note" role="alert">
                          {phaseLabel}读取失败：{c.error}
                        </p>
                      )}
                      {(c.more || c.error) && (
                        <button
                          className="btn"
                          aria-label={label}
                          title={label}
                          disabled={c.busy}
                          onClick={() => load(p)}
                        >
                          {c.error ? (
                            <RefreshCw aria-hidden="true" />
                          ) : (
                            <ArrowDown aria-hidden="true" />
                          )}
                        </button>
                      )}
                    </div>
                  );
                })}
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
      <DetailPanel
        key={id}
        id={id}
        summary={summary}
        tab={tab}
        refresh={refresh}
        onChanged={onChanged}
        onBack={onBack}
        nav={nav}
        onNav={() => setNav(true)}
      />
    </div>
  );
}

function DetailPanel({
  id,
  summary,
  tab,
  refresh,
  onChanged,
  onBack,
  nav,
  onNav,
}: {
  id: number;
  summary?: FlowSummary;
  tab: Tab;
  refresh: number;
  onChanged: (flow: FlowDetail) => void;
  onBack: () => void;
  nav: boolean;
  onNav: () => void;
}) {
  const [freshFlow, setFlow] = useState<FlowDetail | null>(null),
    [error, setError] = useState("");
  // A previously visited conversation can mount while its mutable detail revalidates.
  const flow = freshFlow ?? (tab === "conversation" ? summary : null);
  const showDetail = !!flow && !error;
  const [pending, setPending] = useState<
    "archive" | "resume" | "retry" | "agent-task" | null
  >(null);
  const [acceptedInputs, setAcceptedInputs] = useState(0);
  const lastActivityRefresh = useRef(refresh);
  const writing = useRef(false);
  const [writeError, setWriteError] = useState("");
  const [writeNotice, setWriteNotice] = useState("");
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const submit = async (action: "archive" | "resume" | "retry") => {
    // Fence all writes synchronously, before React renders disabled buttons.
    if (writing.current || !flow || flow.history_only) return;
    writing.current = true;
    setPending(action);
    setWriteError("");
    setWriteNotice("");
    try {
      // Let the write finish after navigation so the board can refresh its committed outcome.
      const value = await (action === "retry"
        ? retryFlow(id)
        : action === "resume"
          ? resumeFlow(id, flow.current_attempt_id)
          : archiveFlow(id, flow.current_attempt_id));
      onChanged(value);
      if (mounted.current) {
        setFlow((prior) =>
          prior && prior.state_version > value.state_version ? prior : value,
        );
        setWriteNotice(
          action === "retry"
            ? "重试请求已受理，请查看当前动作及执行记录。"
            : action === "resume"
              ? "重新开工请求已受理；历史记录已保留，后续将使用新 PR。"
              : "归档请求已受理；停止确认、结果对账及收尾完成后才会归档。",
        );
      }
    } catch (error) {
      if (mounted.current) {
        setWriteError(
          action === "retry"
            ? retryFailureMessage(error)
            : roundFailureMessage(action, error),
        );
      }
    } finally {
      writing.current = false;
      if (mounted.current) setPending(null);
    }
  };
  const [compactHead, setCompactHead] = useState(false);
  const [panes, setPanes] = useState<FilePane[]>([{ files: [], active: null }]);
  const visiblePanes = flow?.history_only
    ? [{ files: [], active: null }]
    : panes;
  const files = visiblePanes.flatMap((pane) => pane.files);
  const activeFile = visiblePanes[0].files.find(
    (file) => `file-${file.id}` === tab,
  );
  const baseTab =
    flowTabs.find(({ value }) => value === tab)?.value ?? "conversation";
  const selectedTab = activeFile ? tab : baseTab;
  const head = useRef<HTMLElement>(null);
  const paneCapacity = () =>
    Math.max(
      1,
      Math.floor((head.current?.getBoundingClientRect().width ?? 0) / 480),
    );
  useLayoutEffect(() => {
    if (!head.current) return;
    const observer = new ResizeObserver(() => {
      const count = paneCapacity();
      setPanes((prior) => {
        if (prior.length <= count) return prior;
        const remaining = prior.slice(count - 1);
        return [
          ...prior.slice(0, count - 1),
          {
            active: remaining[0].active,
            files: remaining.flatMap((pane) => pane.files),
          },
        ];
      });
    });
    observer.observe(head.current);
    return () => observer.disconnect();
  }, [showDetail]);
  const focusTab = useRef<string | null>(null);
  const compactOnFileOpen = useRef(false);
  const openFile = useCallback(
    (file: WorkerFile) => {
      const existing = panes.findIndex((pane) =>
        pane.files.some((item) => item.id === file.id),
      );
      const column =
        existing >= 0 ? existing : Math.min(panes.length, paneCapacity() - 1);
      const next = panes.slice();
      next[column] = {
        files:
          existing >= 0
            ? panes[column].files
            : [...(panes[column]?.files ?? []), file],
        active: column ? file.id : null,
      };
      focusTab.current = `file-${file.id}`;
      setPanes(next);
      if (column === 0) {
        compactOnFileOpen.current =
          head.current?.classList.contains("compact") ?? false;
        location.hash = `flow/${id}/file-${file.id}`;
      }
    },
    [id, panes],
  );
  // Removing a tab from its source has the same neighbor and empty-column rules
  // whether the user drops it in another strip or closes it.
  const placeFile = (fileId: string, destination?: number, before?: string) => {
    const source = panes.findIndex((pane) =>
      pane.files.some((file) => file.id === fileId),
    );
    if (source < 0) return; // A tab can be closed while the browser owns the drag.
    const peers = panes[source].files;
    const index = peers.findIndex((file) => file.id === fileId);
    const file = peers[index];
    if (
      source === destination &&
      (before === file.id || before === peers[index + 1]?.id)
    )
      return;
    const next = panes.slice();
    const neighbor = peers[index + 1] ?? peers[index - 1];
    next[source] = {
      files: peers.filter((item) => item !== file),
      active:
        panes[source].active === fileId
          ? (neighbor?.id ?? null)
          : panes[source].active,
    };
    if (activeFile === file && destination !== 0) {
      const value = neighbor ? `file-${neighbor.id}` : "conversation";
      location.hash = `flow/${id}/${value}`;
      focusTab.current = value;
    } else if (destination === undefined) {
      const other =
        next[source].active ?? next.findLast((pane) => pane.active)?.active;
      focusTab.current = source && other ? `file-${other}` : selectedTab;
    }
    if (destination !== undefined) {
      const group = [...next[destination].files];
      group.splice(
        before ? group.findIndex((item) => item.id === before) : group.length,
        0,
        file,
      );
      next[destination] = {
        files: group,
        active: destination ? file.id : null,
      };
      if (!destination) location.hash = `flow/${id}/file-${file.id}`;
      focusTab.current = `file-${file.id}`;
    }
    setPanes(next.filter((pane, i) => i === 0 || pane.files.length));
  };
  useEffect(() => {
    // Open documents are local to this Flow visit; stale history entries return to the conversation.
    // Check each render: rapid Back can revisit the same tab prop before hashchange settles.
    if (
      tab.startsWith("file-") &&
      !activeFile &&
      location.hash.endsWith(`/${tab}`)
    )
      location.replace(`#flow/${id}/conversation`);
  });
  const expandedHeight = useRef(0);
  const headTransition = useRef<{ height: number; scroll: number } | null>(
    null,
  );
  const expandedAt = useRef<number | null>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]),
    [jobNext, setJobNext] = useState<string | null>(null),
    [jobMore, setJobMore] = useState(true),
    [jobError, setJobError] = useState("");
  const detail = useRef<HTMLDivElement>(null);
  const jobsRequest = useRef<AbortController | null>(null);
  const jobsBusy = useRef(false);
  const [mainFileHost, setMainFileHost] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (compactHead || !focusTab.current) return;
    const trigger = head.current?.querySelector<HTMLButtonElement>(
      `[data-tab="${focusTab.current}"][data-state="active"]`,
    );
    if (!trigger) return;
    focusTab.current = null;
    trigger.focus({ preventScroll: true });
  }, [selectedTab, panes, Boolean(flow), compactHead]);
  useLayoutEffect(() => {
    const node = detail.current;
    if (!node) return;
    if (tab === "conversation") {
      // The conversation restores a logical row after its groups remount folded.
      node.scrollTop = 0;
      return;
    }
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
      node.dispatchEvent(new Event("window-list-navigation"));
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
    let restoreCompact = tab === "conversation" && compactOnFileOpen.current;
    if (tab === "conversation") compactOnFileOpen.current = false;
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
        // The saved conversation offset already accounts for its original compact header.
        headTransition.current =
          restoreCompact && shrink
            ? null
            : {
                height: header.offsetHeight,
                scroll: node.scrollTop,
              };
        setCompactHead(shrink);
      }
      if (shrink) restoreCompact = false;
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
      .then((value) => {
        if (c.signal.aborted) return;
        setFlow((prior) =>
          prior && prior.state_version > value.state_version ? prior : value,
        );
        onChanged(value);
      })
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
  if (!showDetail)
    return (
      <main className="detail">
        <div className="empty-state">
          <button
            className="btn mobile-nav"
            aria-label="切换任务"
            title="切换任务"
            aria-expanded={nav}
            onClick={onNav}
          >
            <Menu aria-hidden="true" />
          </button>
          {error ? (
            <>
              <strong>无法读取 Flow</strong>
              <span>{error}</span>
              <button
                className="btn"
                aria-label="返回看板"
                title="返回看板"
                onClick={onBack}
              >
                <ArrowLeft aria-hidden="true" />
              </button>
            </>
          ) : (
            <p>正在读取 Flow…</p>
          )}
        </div>
      </main>
    );
  return (
    <Tabs
      value={selectedTab}
      onValueChange={(value) => (location.hash = `flow/${id}/${value}`)}
      asChild
    >
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
                title="返回看板"
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
                title="展开详情标题"
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
              aria-label="切换任务"
              title="切换任务"
              aria-expanded={nav}
              onClick={onNav}
            >
              <Menu />
            </button>
            <div className="detail-title-row">
              <h1 id="detail-heading">{name(flow)}</h1>
              <span className={`badge phase-${flow.phase}`}>
                <i className="dot" />
                {flowLabel(flow)}
              </span>
            </div>
            <div className="links">
              <span>{flow.repo}</span>
              {flow.issue_number !== null && flow.issue_url && (
                <a href={flow.issue_url} target="_blank" rel="noreferrer">
                  Issue #{flow.issue_number} ↗
                </a>
              )}
              {flow.issue_number === null && !flow.pr_number && (
                <span>Flow #{flow.id}</span>
              )}
              {flow.pr_url && (
                <a href={flow.pr_url} target="_blank" rel="noreferrer">
                  PR #{flow.pr_number} ↗
                </a>
              )}
            </div>
            <FileTabRow split={visiblePanes.length > 1} move={placeFile}>
              {visiblePanes.map((pane, column) => {
                const list = (
                  <TabsList
                    key="primary"
                    data-file-column={column}
                    aria-label={column ? `文件栏 ${column}` : "Flow 详情"}
                  >
                    {!column &&
                      flowTabs.map(({ value, label }) => (
                        <TabsTrigger value={value} data-tab={value} key={value}>
                          {label}
                        </TabsTrigger>
                      ))}
                    {pane.files.map((file) => (
                      <FileTab
                        key={file.id}
                        file={file}
                        side={column !== 0}
                        duplicate={
                          file.name !== null &&
                          files.some(
                            (other) =>
                              other.id !== file.id && other.name === file.name,
                          )
                        }
                        select={() => openFile(file)}
                        close={() => placeFile(file.id)}
                      />
                    ))}
                  </TabsList>
                );
                return column ? (
                  <Tabs
                    key={column}
                    value={`file-${pane.active}`}
                    onValueChange={(value) =>
                      openFile(
                        pane.files.find((file) => `file-${file.id}` === value)!,
                      )
                    }
                  >
                    {list}
                  </Tabs>
                ) : (
                  list
                );
              })}
            </FileTabRow>
          </div>
        </header>
        <div className="detail-panes">
          <div className="primary-pane">
            <TabsContent
              value={selectedTab}
              className="detail-body"
              data-scroll-root
              ref={detail}
            >
              <section
                className={activeFile ? "content file-content" : "content"}
                ref={setMainFileHost}
              >
                {!activeFile &&
                  (
                    {
                      overview: freshFlow && (
                        <>
                          {!flow.history_only && (
                            <section
                              className={`execution-summary phase-${flow.phase}`}
                            >
                              <span className="eyebrow">当前动作</span>
                              <h2>{actionLabel(flow)}</h2>
                              <p>{freshFlow.reason}</p>
                              <div className="next-step">
                                <span>下一步条件</span>
                                <p>{freshFlow.next_condition}</p>
                              </div>
                              <div className="round-actions">
                                {flow.lifecycle === "blocked" && (
                                  <Button
                                    className="btn"
                                    aria-label={
                                      pending === "retry"
                                        ? "正在提交重试…"
                                        : "重试"
                                    }
                                    title={
                                      pending === "retry"
                                        ? "正在提交重试…"
                                        : "重试"
                                    }
                                    disabled={pending !== null}
                                    onClick={() => void submit("retry")}
                                  >
                                    <RefreshCw />
                                    {pending === "retry" && "正在提交重试…"}
                                  </Button>
                                )}
                                {flow.lifecycle === "archived" ? (
                                  <>
                                    <p id="resume-hint">
                                      重新开工将在同一 Flow
                                      开始新的执行，从待处理开始并使用新
                                      PR，保留历史记录；不会重试已结束的执行。
                                    </p>
                                    <Button
                                      className="btn"
                                      aria-describedby="resume-hint"
                                      disabled={pending !== null}
                                      onClick={() => void submit("resume")}
                                    >
                                      <RefreshCw />
                                      {pending === "resume"
                                        ? "正在重新开工…"
                                        : "重新开工"}
                                    </Button>
                                  </>
                                ) : flow.lifecycle === "finalizing" ? (
                                  <p>
                                    正在结束本次执行。停止确认、结果对账及收尾未完成前不会归档，请刷新查看进展。
                                  </p>
                                ) : (
                                  <Button
                                    className="btn"
                                    disabled={pending !== null}
                                    onClick={() => void submit("archive")}
                                  >
                                    <Archive />
                                    {pending === "archive"
                                      ? "正在请求归档…"
                                      : "归档"}
                                  </Button>
                                )}
                              </div>
                              {writeError && <p role="alert">{writeError}</p>}
                              {writeNotice && (
                                <p role="status">{writeNotice}</p>
                              )}
                            </section>
                          )}
                          {freshFlow.task_content && (
                            <section
                              className="task-content"
                              aria-label="原始任务内容"
                            >
                              <h2 className="panel-title">原始任务内容</h2>
                              <OpenWorkerFile
                                value={
                                  flow.history_only ? "history-only" : null
                                }
                              >
                                <ActivityMarkdown
                                  text={freshFlow.task_content}
                                  url={null}
                                />
                              </OpenWorkerFile>
                            </section>
                          )}
                          <h2 className="panel-title">任务信息</h2>
                          <dl className="task-facts">
                            <dt>生命周期 / 阶段</dt>
                            <dd>
                              {flow.lifecycle} /{" "}
                              {phases.find(([p]) => p === flow.phase)?.[1]}
                            </dd>
                            <dt>开工时间</dt>
                            <dd>
                              {new Date(
                                flow.attempt_started_at,
                              ).toLocaleString()}
                            </dd>
                            {flow.archive_reason && (
                              <>
                                <dt>
                                  {flow.lifecycle === "archived"
                                    ? "归档结果"
                                    : "结束目标"}
                                </dt>
                                <dd>
                                  {archiveReasonLabels[flow.archive_reason]}
                                </dd>
                              </>
                            )}
                            {flow.archive_summary && (
                              <>
                                <dt>本次执行结论</dt>
                                <dd className="round-summary">
                                  {flow.archive_summary}
                                </dd>
                              </>
                            )}
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
                            <dd>
                              {new Date(flow.created_at).toLocaleString()}
                            </dd>
                            <dt>更新</dt>
                            <dd>
                              {new Date(flow.updated_at).toLocaleString()}
                            </dd>
                            {flow.archived_at && (
                              <>
                                <dt>归档时间</dt>
                                <dd>
                                  {new Date(flow.archived_at).toLocaleString()}
                                </dd>
                              </>
                            )}
                            <dt>当前状态版本</dt>
                            <dd>{flow.state_version}（与阅读快照独立）</dd>
                          </dl>
                          <h2 className="panel-title">数据来源</h2>
                          <SourceList sources={freshFlow.sources} />
                        </>
                      ),
                      conversation: (
                        <OpenWorkerFile
                          value={flow.history_only ? "history-only" : openFile}
                        >
                          <Conversation
                            key={id}
                            id={id}
                            root={detail}
                            refresh={
                              flow.history_only
                                ? null
                                : refresh + acceptedInputs
                            }
                            lastRefresh={lastActivityRefresh}
                          />
                        </OpenWorkerFile>
                      ),
                      jobs: (
                        <>
                          <InferenceNotices key={`${id}:${refresh}`} id={id} />
                          <h2 className="panel-title">任务执行</h2>
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
                                      {jobs[i].kind === "inference"
                                        ? `推理 · 派发次数 ${jobs[i].attempt}`
                                        : `${jobs[i].kind} #${jobs[i].attempt}`}
                                    </strong>
                                    <span className="badge">
                                      {jobs[i].state}
                                    </span>
                                  </div>
                                  <time dateTime={jobs[i].created_at}>
                                    创建于{" "}
                                    {new Date(
                                      jobs[i].created_at,
                                    ).toLocaleString()}
                                  </time>
                                  <p>
                                    {jobs[i].worker_id ?? "无 Worker"}
                                    {jobs[i].error_code
                                      ? ` · ${jobs[i].error_code}`
                                      : ""}
                                  </p>
                                </article>
                              )}
                            </WindowList>
                          </div>
                          {jobError && (
                            <button className="btn" onClick={() => moreJobs()}>
                              <RefreshCw aria-hidden="true" />
                              <span>读取失败：{jobError}（重试）</span>
                            </button>
                          )}
                          {jobMore && (
                            <button
                              className="btn"
                              aria-label="读取更多执行记录"
                              title="读取更多执行记录"
                              onClick={() => moreJobs()}
                            >
                              <ArrowDown aria-hidden="true" />
                            </button>
                          )}
                        </>
                      ),
                    } satisfies Record<typeof baseTab, ReactNode>
                  )[baseTab]}
              </section>
            </TabsContent>
          </div>
          {files.map((file) => {
            const index = visiblePanes.findIndex(
              (pane) => pane.active === file.id,
            );
            const target =
              file === activeFile
                ? mainFileHost
                : index !== -1
                  ? "inline"
                  : null;
            const loading = <p role="status">正在加载文件查看器…</p>;
            return (
              <section
                key={file.id}
                className="file-pane"
                style={{ order: index }}
                hidden={index === -1}
                data-scroll-root
                role="tabpanel"
                id={`panel-file-${file.id}`}
                aria-labelledby={`tab-file-${file.id}`}
                tabIndex={0}
              >
                <Suspense
                  fallback={
                    target && target !== "inline"
                      ? createPortal(loading, target)
                      : loading
                  }
                >
                  <FileViewer
                    linkId={file.id}
                    filename={file.name}
                    target={target}
                  />
                </Suspense>
              </section>
            );
          })}
        </div>
        {freshFlow && !flow.history_only && (
          <AgentTaskInput
            busy={pending !== null}
            mode={
              flow.lifecycle === "archived"
                ? "archived"
                : flow.lifecycle === "finalizing" ||
                    flow.archive_reason !== null
                  ? "note"
                  : "active"
            }
            onSubmit={async (instruction, requestId, files) => {
              if (writing.current)
                throw new Error("正在提交其他操作，请稍候。");
              writing.current = true;
              setPending("agent-task");
              try {
                const value = await appendAgentTask(
                  id,
                  instruction,
                  requestId,
                  files,
                );
                onChanged(value);
                if (mounted.current) {
                  setAcceptedInputs((count) => count + 1);
                  setFlow((prior) =>
                    prior && prior.state_version > value.state_version
                      ? prior
                      : value,
                  );
                }
              } finally {
                writing.current = false;
                if (mounted.current) setPending(null);
              }
            }}
          />
        )}
      </main>
    </Tabs>
  );
}

function Conversation({
  id,
  root,
  refresh,
  lastRefresh,
}: {
  id: number;
  root: React.RefObject<HTMLElement | null>;
  refresh: number | null;
  lastRefresh: React.RefObject<number>;
}) {
  const historyOnly = refresh === null;
  const { state, load, onRange, onAnchor, readProvenance, retry } =
    useConversation(id, refresh, lastRefresh);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const sentinel = useRef<HTMLDivElement>(null);
  const automaticBoundary = useRef<string | null>(null);
  const [jumping, setJumping] = useState(false);
  useEffect(() => {
    const viewport = root.current;
    if (!jumping || !viewport) return;
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    const more = state.has_more && !state.error;
    if (reducedMotion.matches && more) {
      if (!state.busy) load();
      return;
    }
    // Follow the growing list; its normal prefetch starts the next page as we approach it.
    let previous = performance.now();
    let settled = 0;
    let frame: number;
    const advance = (now: number) => {
      const remaining =
        viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop;
      if (remaining > 1) {
        // Limit travel per frame and ease into the tail as virtual rows settle.
        const elapsed = Math.min(now - previous, 32);
        const speed = Math.min(viewport.clientHeight / 80, remaining / 100);
        viewport.scrollTop += reducedMotion.matches
          ? remaining
          : Math.min(remaining, Math.max(1, speed * elapsed));
        settled = 0;
      } else if (++settled >= 2) {
        if (!more && !state.busy) setJumping(false);
        return; // A pending page will restart the animation when it arrives.
      }
      previous = now;
      frame = requestAnimationFrame(advance);
    };
    frame = requestAnimationFrame(advance);
    return () => cancelAnimationFrame(frame);
  }, [
    jumping,
    state.busy,
    state.has_more,
    state.error,
    state.next_cursor,
    load,
    root,
  ]);
  const rows = useMemo(
    () => buildConversationRows(state.records, expanded),
    [state.records, expanded],
  );
  const rowByKey = useMemo(
    () => new Map(rows.map((row) => [row.key, row])),
    [rows],
  );
  const mountedRange = useCallback(
    (indexes: number[]) => {
      onRange(
        indexes.flatMap((index) => {
          const row = rows[index];
          return row.kind === "body" ? [row.rawIndex] : [];
        }),
      );
    },
    [rows, onRange],
  );
  const visibleRange = useCallback(
    (keys: readonly (string | number)[], anchorKey: string | number | null) => {
      const members = new Map<string, string[]>();
      const summaries = new Set<string>();
      for (const key of keys) {
        const row = typeof key === "string" ? rowByKey.get(key) : undefined;
        const group = row?.kind === "summary" ? row.group.key : row?.groupKey;
        if (!row || !group) continue;
        const index = row.kind === "summary" ? row.group.first : row.rawIndex;
        const visible = members.get(group) ?? [];
        visible.push(state.records[index].key);
        members.set(group, visible);
        if (row.kind === "summary") summaries.add(group);
      }
      setExpanded((before) => {
        const next = new Set<string>();
        // A late visibility report must not reopen a group the user has just collapsed.
        for (const key of before) {
          const row = rowByKey.get(conversationRowKey("body", key));
          const group = row?.kind === "body" ? row.groupKey : null;
          const visible = group ? members.get(group) : undefined;
          if (!group || !visible) continue;
          visible.forEach((member) => next.add(member));
          if (summaries.has(group)) next.add(key);
        }
        if (
          before.size === next.size &&
          [...before].every((key) => next.has(key))
        )
          return before;
        return next;
      });
      const anchor =
        typeof anchorKey === "string" ? rowByKey.get(anchorKey) : undefined;
      const index =
        anchor?.kind === "summary" ? anchor.group.first : anchor?.rawIndex;
      onAnchor(index === undefined ? null : state.records[index].key);
    },
    [rowByKey, state.records, onAnchor],
  );
  useEffect(() => {
    if (state.snapshot === null || state.busy || state.error || !state.has_more)
      return;
    const node = sentinel.current;
    const viewport = root.current;
    if (!node || !viewport) return;
    // Folded groups can absorb a whole page without adding display rows or moving the sentinel.
    const boundary = `${state.snapshot}:${state.next_cursor}`;
    if (automaticBoundary.current === boundary) return;
    let observer: IntersectionObserver | undefined;
    // Recalculate the reading runway when the mobile header changes the viewport.
    const resize = new ResizeObserver(() => {
      observer?.disconnect();
      observer = new IntersectionObserver(
        (entries) => {
          if (
            entries.some((entry) => entry.isIntersecting) &&
            automaticBoundary.current !== boundary &&
            load()
          )
            automaticBoundary.current = boundary;
        },
        {
          root: viewport,
          rootMargin: `0px 0px ${viewport.clientHeight * 4}px 0px`,
        },
      );
      observer.observe(node);
    });
    resize.observe(viewport);
    return () => {
      resize.disconnect();
      observer?.disconnect();
    };
  }, [
    load,
    root,
    state.snapshot,
    state.busy,
    state.error,
    state.has_more,
    state.next_cursor,
  ]);
  return (
    <>
      {!historyOnly && (
        <ConversationSources
          sources={state.sources}
          busy={state.busy}
          refresh={() => load(true)}
        />
      )}
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
        {state.snapshot !== null && (
          <WindowList
            count={rows.length}
            label="会话活动"
            itemKey={(i) => rows[i].key}
            onRange={mountedRange}
            onVisibleRange={visibleRange}
            reading={{
              key: `conversation:${id}`,
              resolve: (key) => {
                if (typeof key !== "string") return null;
                const rawKey = conversationRecordKey(key);
                const index = state.records.findIndex(
                  (entry) => entry.key === rawKey,
                );
                return (
                  rows.find(
                    (row) =>
                      row.kind === "summary" &&
                      row.group.first <= index &&
                      index <= row.group.last,
                  )?.key ?? null
                );
              },
            }}
            estimate={220}
          >
            {(i) => {
              const row = rows[i];
              if (row.kind === "summary") {
                const entry = state.records[row.group.first];
                return (
                  <ActivitySummary
                    entry={entry}
                    group={row.group}
                    expanded={row.expanded}
                    continuation={sameActivityGroup(
                      state.records[row.group.first - 1],
                      entry,
                    )}
                    onToggle={() =>
                      setExpanded((before) => {
                        const next = new Set(before);
                        if (row.expanded)
                          for (
                            let index = row.group.first;
                            index <= row.group.last;
                            index++
                          )
                            next.delete(state.records[index].key);
                        else next.add(entry.key);
                        return next;
                      })
                    }
                  />
                );
              }
              const e = state.items.get(row.rawIndex);
              const identity = state.records[row.rawIndex];
              const provenance = conversationProvenanceRange(
                state.records,
                row.rawIndex,
              );
              return e ? (
                <ActivityView
                  key={`${e.key}-${e.version}`}
                  entry={e}
                  flowId={id}
                  snapshot={state.snapshot ?? 0}
                  continuation={sameActivityGroup(
                    state.records[row.rawIndex - 1],
                    identity,
                  )}
                  groupKey={row.groupKey}
                  pairedCallKey={row.pairedCallKey}
                  provenance={
                    provenance
                      ? { ...provenance, read: readProvenance }
                      : undefined
                  }
                />
              ) : (
                <div className="history-placeholder">正在读取历史交互…</div>
              );
            }}
          </WindowList>
        )}
      </div>
      {state.error ? (
        <button className="btn" onClick={retry}>
          <RefreshCw aria-hidden="true" />
          <span>读取失败：{state.error.message}（重试）</span>
        </button>
      ) : state.has_more ? (
        <div className="load-sentinel" role="status" ref={sentinel}>
          <button
            className="btn"
            aria-label={state.busy ? "正在加载更多交互…" : "读取更多交互"}
            title={state.busy ? "正在加载更多交互…" : "读取更多交互"}
            aria-disabled={state.busy}
            onClick={() => load()}
          >
            <ArrowDown aria-hidden="true" />
            {state.busy && "正在加载更多交互…"}
          </button>
        </div>
      ) : historyOnly ? (
        <p className="conversation-divider">已到保存记录末尾</p>
      ) : (
        <button
          className="refresh-boundary conversation-divider"
          disabled={state.busy}
          onClick={() => void load(true)}
        >
          <RefreshCw aria-hidden="true" />
          {state.busy ? "正在检查更新…" : "已到当前本地快照末尾 · 点击刷新"}
        </button>
      )}
      {state.records.length > 0 && !state.error && (
        <button
          type="button"
          className="btn jump-to-latest"
          aria-label="跳到最新回复"
          title="跳到最新回复"
          aria-busy={jumping}
          disabled={jumping}
          onClick={() => setJumping(true)}
        >
          {jumping ? (
            <LoaderCircle
              className="motion-safe:animate-spin"
              aria-hidden="true"
            />
          ) : (
            <ArrowDownToLine aria-hidden="true" />
          )}
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
    [settingsRefresh, setSettingsRefresh] = useState(0),
    [boardStarted, setBoardStarted] = useState(!current.settings),
    [mobilePhase, setMobilePhase] = useState<Phase>("running"),
    [boardRevision, setBoardRevision] = useState(0),
    [pages, setColumns] = useState(blank),
    [pageCounts, setCounts] = useState(
      Object.fromEntries(phases.map(([p]) => [p, 0])) as Record<Phase, number>,
    ),
    [updates, setUpdates] = useState(new Map<number, FlowSummary>());
  const updateSummary = (summary: FlowSummary) => {
    setUpdates((old) =>
      (old.get(summary.id)?.state_version ?? -1) >= summary.state_version
        ? old
        : new Map(old).set(summary.id, summary),
    );
  };
  // Keep page cursors and server snapshots intact, including pages arriving after the detail.
  const { columns, counts } = useMemo(() => {
    const columns = { ...pages },
      counts = { ...pageCounts };
    const search = parseSearch(query, []);
    for (const [phase] of phases) {
      for (const row of pages[phase].items) {
        const updated = updates.get(row.id);
        if (!updated || updated.state_version <= row.state_version) continue;
        const visible = search.ok && matchesSearch(search.query, updated);
        const source = columns[phase];
        if (visible && phase === updated.phase) {
          columns[phase] = {
            ...source,
            items: source.items.map((f) => (f.id === row.id ? updated : f)),
          };
          continue;
        }
        columns[phase] = {
          ...source,
          items: source.items.filter((f) => f.id !== row.id),
        };
        counts[phase]--;
        const dest = columns[updated.phase];
        if (visible && !dest.items.some((f) => f.id === row.id)) {
          columns[updated.phase] = {
            ...dest,
            items: [...dest.items, updated].sort((a, b) => b.id - a.id),
          };
          counts[updated.phase]++;
        }
      }
    }
    return { columns, counts };
  }, [pages, pageCounts, updates, query]);
  const boardRequests = useRef(new Map<Phase, AbortController>());
  useEffect(() => {
    const sync = () => {
      setCurrent(route());
      setQuery(new URL(location.href).searchParams.get("q") ?? "");
    };
    addEventListener("hashchange", sync);
    addEventListener("popstate", sync);
    return () => {
      removeEventListener("hashchange", sync);
      removeEventListener("popstate", sync);
    };
  }, []);
  useEffect(() => {
    if (!current.settings) setBoardStarted(true);
  }, [current.settings]);
  useEffect(() => {
    if (!boardStarted) return;
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
  }, [boardStarted]);
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
        const first = reset || prior.next === null;
        if (first) setCounts((x) => ({ ...x, [phase]: page.counts[phase] }));
        // A flow moved here can reappear below the old cursor. Keep its original
        // list membership as the baseline for the count adjustment.
        let items = page.items;
        if (!first) {
          const known = new Set(
            phases.flatMap(([p]) => pages[p].items.map((f) => f.id)),
          );
          items = items.filter((item) => {
            if (!known.has(item.id)) return true;
            updateSummary(item);
            return false;
          });
        }
        setColumns((x) => ({
          ...x,
          [phase]: {
            items: first ? items : [...x[phase].items, ...items],
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
    if (!boardStarted) return;
    setUpdates(new Map());
    phases.forEach(([p]) => load(p, true));
    return () => boardRequests.current.forEach((c) => c.abort());
  }, [query, boardRevision, boardStarted]);
  const changeQuery = (value: string) => {
    // Hash navigation in the same action must already see the new query in the URL.
    const url = new URL(location.href);
    if (value) url.searchParams.set("q", value);
    else url.searchParams.delete("q");
    history.replaceState(null, "", url);
    setQuery(value);
  };
  return (
    <>
      <header className="workspace-bar">
        <nav className="bar-path">
          <span>工作空间</span>
          <span>/</span>
          <strong>
            {current.settings ? "设置" : current.id ? "任务详情" : "看板"}
          </strong>
        </nav>
        <div className="bar-filters" hidden={current.settings}>
          <SearchBox query={query} repos={repos} onQuery={changeQuery} />
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
              {repoError ? (
                <RefreshCw aria-hidden="true" />
              ) : (
                <ArrowDown aria-hidden="true" />
              )}
              <span>
                {repoError ? `仓库读取失败：${repoError}（重试）` : "更多仓库"}
              </span>
            </button>
          )}
        </div>
        <div className="bar-status">
          <Button asChild className="btn ghost">
            <a
              href="#settings"
              aria-current={current.settings ? "page" : undefined}
              aria-label="设置"
              title="设置"
            >
              <Settings aria-hidden="true" />
            </a>
          </Button>
          <CreateFlow
            onCreated={(id) => {
              setBoardRevision((x) => x + 1);
              openFlow(id);
            }}
            onVerify={() => {
              changeQuery("");
              setBoardRevision((x) => x + 1);
              location.hash = "board";
            }}
          />
          <button
            className="btn ghost"
            aria-label="刷新当前视图"
            title="刷新当前视图"
            onClick={() => {
              if (current.settings) setSettingsRefresh((x) => x + 1);
              else {
                if (current.id) setRefresh((x) => x + 1);
                setBoardRevision((x) => x + 1);
              }
            }}
          >
            <RefreshCw />
          </button>
        </div>
      </header>
      <SettingsPage
        active={current.settings}
        refresh={settingsRefresh}
        onBack={() => (location.hash = "board")}
      />
      {current.settings ? null : current.id ? (
        <Detail
          id={current.id}
          summary={updates.get(current.id)}
          tab={current.tab}
          columns={columns}
          counts={counts}
          load={load}
          refresh={refresh}
          onChanged={({
            task_content,
            reason,
            next_condition,
            sources,
            ...summary
          }) => updateSummary(summary)}
          onBack={() => (location.hash = "board")}
        />
      ) : (
        <Board
          columns={columns}
          counts={counts}
          load={load}
          go={openFlow}
          mobile={mobilePhase}
          onMobile={setMobilePhase}
        />
      )}
    </>
  );
}
