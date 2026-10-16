import {
  type KeyboardEvent,
  type Ref,
  type RefObject,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { RefreshCw } from "lucide-react";
import { flows } from "./fixtures";
import { phases } from "./phases";
import type { Flow, Tab } from "./types";
import { ActivityView } from "./activity";
import { useConversation } from "./useConversation";
import { Board, DetailHeader, Jobs, Overview, Rail } from "./views";
import { Button } from "./components/ui/button";
import { SearchBox } from "./SearchBox";
import { matchesFlow, parseSearch } from "./search";
import { WindowList } from "./windowing";
import { focusableControls } from "./focus";

const parseRoute = (): { flow: Flow | null; tab: Tab; invalid: boolean } => {
  const match = location.hash.match(
    /^#flow\/(\d+)(?:\/(overview|conversation|jobs))?$/,
  );
  return {
    flow: match
      ? (flows.find((item) => item.id === Number(match[1])) ?? null)
      : null,
    tab:
      match?.[2] === "overview"
        ? "overview"
        : match?.[2] === "jobs"
          ? "jobs"
          : "conversation",
    invalid: match
      ? !flows.some((item) => item.id === Number(match[1]))
      : Boolean(location.hash && location.hash !== "#board"),
  };
};

function readFilters() {
  const params = new URL(location.href).searchParams;
  const phase = params.get("phase") || "running";
  return {
    query: params.get("q") || "",
    phase: phases.some(([key]) => key === phase) ? phase : "running",
  };
}

type ConversationHandle = { refresh: () => void };
function Conversation({
  flow,
  root,
  ref,
}: {
  flow: Flow;
  root: RefObject<HTMLElement | null>;
  ref: Ref<ConversationHandle>;
}) {
  const { state, advance, onRange, retry } = useConversation(flow, root);
  useImperativeHandle(
    ref,
    () => ({
      refresh: () => {
        if (!state.hasMore) void advance(true);
      },
    }),
    [advance, state.hasMore],
  );
  return (
    <>
      <div className="conversation-sources">
        <span className="source-state">
          <i className="dot" />
          GitHub · 可读取
        </span>
        <span
          className={`source-state${flow.phase === "blocked" || flow.phase === "queued" ? " unavailable" : ""}`}
        >
          <i className="dot" />
          Worker ·{" "}
          {flow.phase === "blocked"
            ? "暂时离线"
            : flow.phase === "queued"
              ? "尚无会话"
              : "历史可读取"}
        </span>
        <span className="sample-hint">全部为示例 · 下滑加载更多</span>
      </div>
      {flow.phase === "blocked" ? (
        <div className="notice">
          <span>
            <strong>Worker 离线，新执行记录暂不可读取</strong>
            <br />
            已有历史样例仍保留，GitHub 交互可独立更新。
          </span>
        </div>
      ) : flow.phase === "queued" ? (
        <div className="notice">
          Agent 会话尚未开始，已有关联 Issue 的交互。
        </div>
      ) : null}
      <div className="date-label">
        2026 年 10 月 16 日 · 关联 Issue / PR 与 Agent 会话
      </div>
      <div
        className="conversation-stream"
        data-flow-id={flow.id}
        data-cached-rows={state.items.size}
      >
        <WindowList
          count={state.count}
          label="会话活动"
          itemKey={(index) => `${flow.id}-activity-${index}`}
          onRange={onRange}
          estimate={250}
        >
          {(index) => {
            const entry = state.items.get(index);
            return (
              <>
                {state.newStart === index && (
                  <div
                    className="new-records conversation-divider"
                    role="status"
                  >
                    本次新增交互
                  </div>
                )}
                {entry ? (
                  <ActivityView
                    entry={entry}
                    stateKey={`${flow.id}-${entry.id}`}
                  />
                ) : (
                  <div className="history-placeholder" role="status">
                    正在读取第 {index + 1} 条历史交互…
                  </div>
                )}
              </>
            );
          }}
        </WindowList>
      </div>
      <div className="conversation-tail" aria-live="polite">
        {state.error ? (
          <>
            <button
              className="refresh-boundary conversation-divider"
              data-action="conversation-retry"
              onClick={retry}
            >
              读取失败 · 点击重试
            </button>
            <p className="boundary-caption" role="alert">
              {state.error}
            </p>
          </>
        ) : state.hasMore ? (
          <div className="load-sentinel" role="status">
            {state.busy ? "正在加载更多交互…" : "向下滚动，继续读取交互"}
          </div>
        ) : (
          <>
            <button
              className="refresh-boundary conversation-divider"
              data-action="conversation-refresh"
              disabled={state.busy}
              onClick={() => void advance(true)}
            >
              <span>
                {state.busy
                  ? "正在检查更新…"
                  : state.checked
                    ? "暂无新交互 · 点击再次检查"
                    : `已加载到 ${state.latestTime ?? "当前读取位置"} · 点击刷新`}
              </span>
            </button>
            <p className="boundary-caption">
              仅表示本次读取位置 ·{" "}
              {flow.phase === "blocked"
                ? "Worker 暂时离线，仍可获取 GitHub 交互"
                : "不代表任务执行结束"}
            </p>
          </>
        )}
      </div>
    </>
  );
}

export default function App() {
  const [route, setRoute] = useState(parseRoute),
    [query, setQuery] = useState(() => readFilters().query),
    [mobilePhase, setMobilePhase] = useState(() => readFilters().phase),
    [collapsed, setCollapsed] = useState(false),
    [navOpen, setNavOpen] = useState(false),
    [toast, setToast] = useState("");
  const detail = useRef<HTMLDivElement>(null);
  const rail = useRef<HTMLElement>(null);
  const conversation = useRef<ConversationHandle>(null);
  const boardScroll = useRef(new Map<string, number>());
  const previousRoute = useRef(route);
  useEffect(() => {
    const sync = () => {
      setRoute(parseRoute());
      const filters = readFilters();
      setQuery(filters.query);
      setMobilePhase(filters.phase);
      setNavOpen(false);
    };
    addEventListener("hashchange", sync);
    addEventListener("popstate", sync);
    return () => {
      removeEventListener("hashchange", sync);
      removeEventListener("popstate", sync);
    };
  }, []);
  useEffect(() => {
    const url = new URL(location.href);
    (
      [
        ["q", query, ""],
        ["phase", mobilePhase, "running"],
      ] as const
    ).forEach(([key, value, base]) =>
      value === base
        ? url.searchParams.delete(key)
        : url.searchParams.set(key, value),
    );
    history.replaceState(null, "", url);
  }, [query, mobilePhase]);
  useEffect(() => {
    const close = () => setNavOpen(false);
    const media = matchMedia("(max-width: 760px)");
    media.addEventListener("change", close);
    return () => media.removeEventListener("change", close);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 2600);
    return () => clearTimeout(timer);
  }, [toast]);
  useLayoutEffect(() => {
    const before = previousRoute.current;
    previousRoute.current = route;
    if (before.flow?.id === route.flow?.id && before.tab === route.tab) return;
    if (!route.flow) {
      let focusFrame = 0;
      // Virtual lists need their scroll roots measured before restoring each column.
      const scrollFrame = requestAnimationFrame(() => {
        for (const [phase, top] of boardScroll.current)
          document
            .querySelector(`.column-scroll[data-phase="${phase}"]`)
            ?.scrollTo(0, top);
        focusFrame = requestAnimationFrame(() => {
          document
            .querySelector<HTMLElement>(`.card[data-flow="${before.flow?.id}"]`)
            ?.focus({ preventScroll: true });
        });
      });
      return () => {
        cancelAnimationFrame(scrollFrame);
        cancelAnimationFrame(focusFrame);
      };
    } else if (before.flow?.id !== route.flow.id) {
      if (detail.current) detail.current.scrollTop = 0;
      document.getElementById("detail-heading")?.focus({ preventScroll: true });
    } else
      document
        .getElementById(`tab-${route.tab}`)
        ?.focus({ preventScroll: true });
  }, [route.flow?.id, route.tab]);
  useEffect(() => {
    if (!navOpen) return;
    rail.current?.querySelector<HTMLButtonElement>(".back")?.focus();
    const keydown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setNavOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const controls = focusableControls(rail.current);
      const first = controls[0],
        last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      document
        .querySelector<HTMLButtonElement>('[data-action="nav"]')
        ?.focus({ preventScroll: true });
    };
  }, [navOpen]);
  useEffect(() => {
    document.title = route.flow
      ? `Flow ${route.flow.id} · AgentFlow 前端预览`
      : "工作看板 · AgentFlow 前端预览";
  }, [route.flow]);
  const repos = [...new Set(flows.map((flow) => flow.repo))];
  const parsed = parseSearch(query, repos);
  const visible = parsed.ok
    ? flows.filter((flow) => matchesFlow(flow, parsed.query))
    : [];
  const go = (
    id: number,
    tab: Tab = route.flow ? route.tab : "conversation",
  ) => {
    if (!route.flow)
      boardScroll.current = new Map(
        phases.map(([phase]) => [
          phase,
          document.querySelector(`.column-scroll[data-phase="${phase}"]`)
            ?.scrollTop ?? 0,
        ]),
      );
    location.hash = `flow/${id}/${tab}`;
    setNavOpen(false);
  };
  const keyTabs = (event: KeyboardEvent) => {
    if (
      !route.flow ||
      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
    )
      return;
    event.preventDefault();
    const all: Tab[] = ["overview", "conversation", "jobs"],
      index = all.indexOf(route.tab);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? 2
          : (index + (event.key === "ArrowRight" ? 1 : 2)) % 3;
    location.hash = `flow/${route.flow.id}/${all[next]}`;
  };
  const copy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setToast("交付分支已复制");
    } catch {
      setToast("浏览器未允许复制，请选中分支文本手动复制");
    }
  };
  return (
    <>
      <header className="workspace-bar" inert={navOpen}>
        <nav className="bar-path" aria-label="当前位置">
          <span>工作空间</span>
          <span>/</span>
          <strong>{route.flow ? `Flow ${route.flow.id}` : "看板"}</strong>
        </nav>
        <div className="bar-filters">
          <SearchBox
            query={query}
            repos={repos}
            onQuery={(value) => {
              if (value === query) return;
              const url = new URL(location.href);
              if (value) url.searchParams.set("q", value);
              else url.searchParams.delete("q");
              history.pushState(null, "", url);
              setQuery(value);
            }}
          />
        </div>
        <div className="bar-status">
          <span className="sync-state" title="后端尚未接入，不代表真实服务在线">
            前端预览 · 示例数据
          </span>
          <Button
            className="btn"
            data-action="refresh"
            onClick={() => {
              setToast("示例快照已刷新 · 未请求真实服务");
              conversation.current?.refresh();
            }}
          >
            <RefreshCw />
            刷新
          </Button>
        </div>
      </header>
      {route.flow ? (
        <div
          className={`detail-shell${collapsed ? " rail-collapsed" : ""}${navOpen ? " nav-open" : ""}`}
        >
          <Rail
            ref={rail}
            navOpen={navOpen}
            visible={visible}
            selected={route.flow.id}
            collapsed={collapsed}
            onBoard={() => {
              setNavOpen(false);
              location.hash = "board";
            }}
            onToggle={() => setCollapsed((value) => !value)}
            onFlow={(id) => go(id)}
          />
          <button
            className="mobile-shade"
            data-action="close-nav"
            aria-label="关闭任务导航"
            onClick={() => setNavOpen(false)}
          />
          <main className="detail" inert={navOpen || undefined}>
            <DetailHeader
              flow={route.flow}
              tab={route.tab}
              navOpen={navOpen}
              onTab={(tab) => {
                if (route.flow) location.hash = `flow/${route.flow.id}/${tab}`;
              }}
              onNav={() => setNavOpen(true)}
              onTabKeyDown={keyTabs}
              onSampleLink={() => setToast("示例链接 · 未连接真实 GitHub")}
            />
            <div className="detail-body" data-scroll-root ref={detail}>
              <section
                className="content"
                role="tabpanel"
                id="detail-panel"
                tabIndex={-1}
                aria-labelledby={`tab-${route.tab}`}
              >
                <div id="filter-notice">
                  {visible.some((f) => f.id === route.flow?.id) ? (
                    ""
                  ) : (
                    <div className="notice">
                      当前任务不在筛选结果中，仍为你保留详情。
                    </div>
                  )}
                </div>
                {route.tab === "overview" ? (
                  <Overview flow={route.flow} onCopy={copy} />
                ) : route.tab === "jobs" ? (
                  <Jobs flow={route.flow} />
                ) : (
                  <Conversation
                    key={route.flow.id}
                    flow={route.flow}
                    root={detail}
                    ref={conversation}
                  />
                )}
              </section>
            </div>
          </main>
        </div>
      ) : route.invalid ? (
        <main className="empty-state">
          <strong>找不到这个示例页面</strong>
          <Button
            className="btn"
            data-action="board"
            onClick={() => {
              location.hash = "board";
            }}
          >
            返回看板
          </Button>
        </main>
      ) : (
        <Board
          visible={visible}
          mobilePhase={mobilePhase}
          onPhase={setMobilePhase}
          onFlow={go}
        />
      )}
      <div id="announcer" role="status" aria-live="polite">
        {toast && <div className="toast">{toast}</div>}
      </div>
    </>
  );
}
