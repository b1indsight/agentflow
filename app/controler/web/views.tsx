import { type KeyboardEventHandler, type Ref, useState } from "react";
import {
  ArrowLeft,
  ChevronRight,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import { phases } from "./phases";
import type { Flow, Tab } from "./types";
import { Button } from "./components/ui/button";
import { labelPreview, TextWindow, WindowList } from "./windowing";

export const reference = (flow: Flow) =>
  flow.pr ? `PR #${flow.pr}` : `Issue #${flow.issue}`;

export function Board({
  visible,
  mobilePhase,
  onPhase,
  onFlow,
}: {
  visible: Flow[];
  mobilePhase: string;
  onPhase: (phase: string) => void;
  onFlow: (id: number) => void;
}) {
  return (
    <main className="board-page" aria-label="工作看板">
      <div className="mobile-stages">
        {phases.map(([key, label]) => (
          <button
            className={`phase-${key}${mobilePhase === key ? " active" : ""}`}
            data-phase={key}
            aria-pressed={mobilePhase === key}
            onClick={() => onPhase(key)}
            key={key}
          >
            <i className="dot" />
            {label}
            <span>{visible.filter((f) => f.phase === key).length}</span>
          </button>
        ))}
      </div>
      <section className="kanban" aria-label="Flow 看板">
        {phases.map(([key, label]) => {
          const group = visible.filter((f) => f.phase === key);
          return (
            <section
              className={`column phase-${key}${mobilePhase === key ? " mobile-selected" : ""}`}
              aria-label={label}
              key={key}
            >
              <div className="col-header">
                <i className="dot" />
                {label}
                <span className="count">{group.length}</span>
              </div>
              <div
                className="column-scroll"
                data-scroll-root
                data-phase={key}
                role="region"
                aria-label={`${label}任务列表`}
                tabIndex={0}
              >
                {!group.length && <div className="empty-col">暂无任务</div>}
                <WindowList
                  count={group.length}
                  label={`看板-${key}`}
                  itemKey={(index) => group[index].id}
                  estimate={270}
                >
                  {(index) => {
                    const flow = group[index];
                    return (
                      <button
                        className={`card phase-${flow.phase}`}
                        data-flow={flow.id}
                        aria-label={`打开 ${reference(flow)}：${labelPreview(flow.title)}`}
                        onClick={() => onFlow(flow.id)}
                        key={flow.id}
                      >
                        <div className="card-top">
                          <span>{reference(flow)}</span>
                          <ChevronRight aria-hidden="true" />
                        </div>
                        <h2>{labelPreview(flow.title)}</h2>
                        <div className="repo-label">
                          b1indsight / {labelPreview(flow.repo)}
                        </div>
                        {flow.note && (
                          <div className="card-note">
                            {labelPreview(flow.note)}
                          </div>
                        )}
                        <div className="card-foot">
                          <span className="current-action">
                            <i className="dot" />
                            {labelPreview(flow.action)}
                          </span>
                          {flow.worker && flow.phase !== "done" && (
                            <span className="worker-pill">
                              {labelPreview(flow.worker, 40)}
                            </span>
                          )}
                        </div>
                        <div className="card-age">{flow.age}</div>
                      </button>
                    );
                  }}
                </WindowList>
              </div>
            </section>
          );
        })}
      </section>
      <footer className="footnote">
        <span>点击卡片查看任务详情与会话 · 所有内容均为示例数据</span>
        <span>只读预览 · 不支持拖拽修改</span>
      </footer>
    </main>
  );
}

export function Rail({
  visible,
  selected,
  collapsed,
  onBoard,
  onToggle,
  onFlow,
  navOpen,
  ref,
}: {
  visible: Flow[];
  selected: number;
  collapsed: boolean;
  onBoard: () => void;
  onToggle: () => void;
  onFlow: (id: number) => void;
  navOpen: boolean;
  ref: Ref<HTMLElement>;
}) {
  const [groups, setGroups] = useState<Record<string, boolean>>({});
  const label = collapsed ? "展开任务侧栏" : "折叠任务侧栏";
  return (
    <aside
      ref={ref}
      className="rail"
      aria-label="Flow 导航"
      role={navOpen ? "dialog" : undefined}
      aria-modal={navOpen || undefined}
    >
      <div className="rail-head">
        <Button
          className="btn ghost back"
          data-action="board"
          onClick={onBoard}
        >
          <ArrowLeft />
          返回看板
        </Button>
        <Button
          className="btn rail-toggle"
          data-action="toggle-rail"
          aria-controls="flow-navigation"
          aria-expanded={!collapsed}
          aria-label={label}
          title={label}
          onClick={onToggle}
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        </Button>
      </div>
      <nav className="rail-scroll" id="flow-navigation" data-scroll-root>
        {phases.map(([key, label]) => {
          const group = visible.filter((f) => f.phase === key);
          return (
            <details
              className={`rail-group phase-${key}`}
              open={groups[key] ?? true}
              key={key}
              onToggle={(event) => {
                const open = event.currentTarget.open;
                setGroups((current) =>
                  current[key] === open ? current : { ...current, [key]: open },
                );
              }}
            >
              <summary>
                {label}
                <span className="count">{group.length}</span>
              </summary>
              {!group.length && <div className="empty-col">无任务</div>}
              <WindowList
                count={group.length}
                label={`侧栏-${key}`}
                itemKey={(index) => group[index].id}
                estimate={86}
              >
                {(index) => {
                  const flow = group[index];
                  return (
                    <button
                      className={`flow-tab${selected === flow.id ? " active" : ""}`}
                      data-flow={flow.id}
                      aria-current={selected === flow.id ? "page" : undefined}
                      onClick={() => onFlow(flow.id)}
                      key={flow.id}
                    >
                      <i className="dot" />
                      <span>
                        <span className="name">{labelPreview(flow.title)}</span>
                        <span className="meta mono">{reference(flow)}</span>
                      </span>
                    </button>
                  );
                }}
              </WindowList>
            </details>
          );
        })}
      </nav>
      <div className="rail-footer">{visible.length} 个任务 · 示例数据</div>
    </aside>
  );
}

export function Overview({
  flow,
  onCopy,
}: {
  flow: Flow;
  onCopy: (value: string) => void;
}) {
  return (
    <>
      <section
        className={`execution-summary phase-${flow.phase}`}
        aria-label="当前执行情况"
      >
        <span className="eyebrow">当前动作</span>
        <h2>
          <i className="dot" />
          <TextWindow
            text={flow.phase === "done" ? "成功 · 已归档" : flow.action}
            stateKey={`${flow.id}-action`}
          />
        </h2>
        <p className="summary-reason">
          <TextWindow text={flow.reason} stateKey={`${flow.id}-reason`} />
        </p>
        <div className="next-step">
          <span>下一步条件</span>
          <p>
            <TextWindow
              text={flow.nextCondition}
              stateKey={`${flow.id}-condition`}
            />
          </p>
        </div>
      </section>
      <h2 className="panel-title">执行信息</h2>
      <dl className="execution-info">
        <div className="kv">
          <dt>交付分支</dt>
          <dd>
            {flow.branch ? (
              <>
                <code className="mono">
                  <TextWindow
                    text={flow.branch}
                    stateKey={`${flow.id}-branch`}
                  />
                </code>
                <Button
                  className="btn"
                  data-action="copy-branch"
                  data-value={flow.branch}
                  aria-label="复制交付分支"
                  onClick={() => onCopy(flow.branch!)}
                >
                  复制
                </Button>
              </>
            ) : (
              <span className="muted">尚未创建</span>
            )}
          </dd>
        </div>
        <div className="kv">
          <dt>{flow.phase === "done" ? "最后执行 Worker" : "Worker"}</dt>
          <dd>
            <TextWindow
              text={flow.worker || "待分配"}
              stateKey={`${flow.id}-worker`}
            />
          </dd>
        </div>
        <div className="kv">
          <dt>修订</dt>
          <dd>{flow.cycles ? `${flow.cycles} 轮` : "尚无修订"}</dd>
        </div>
      </dl>
      <h2 className="panel-title">
        {flow.progress.length ? "关键进展" : "等待执行"}
      </h2>
      {flow.progress.length ? (
        <div className="timeline" role="list">
          <WindowList
            count={flow.progress.length}
            label="关键进展"
            itemKey={(index) => `${flow.id}-progress-${index}`}
            estimate={110}
          >
            {(index) => (
              <div className="timeline-item" role="listitem">
                <TextWindow
                  text={flow.progress[index].title}
                  stateKey={`${flow.id}-progress-${index}-label`}
                />
                <p>
                  <TextWindow
                    text={flow.progress[index].description}
                    stateKey={`${flow.id}-progress-${index}-note`}
                  />
                </p>
              </div>
            )}
          </WindowList>
        </div>
      ) : (
        <div className="notice">
          任务已接收，等待可用 Worker。尚无执行或交付记录。
        </div>
      )}
    </>
  );
}

export function Jobs({ flow }: { flow: Flow }) {
  return !flow.records.length ? (
    <div className="empty-state">
      <strong>等待分配 Worker</strong>尚未开始执行，任务已进入队列。
    </div>
  ) : (
    <>
      <h2 className="panel-title">
        执行记录 <span className="tag">示例记录</span>
      </h2>
      <p className="panel-note">
        展示首轮、最近一轮修订及归档的示例，不代表完整历史。
      </p>
      <WindowList
        count={flow.records.length}
        label="执行记录"
        itemKey={(index) => `${flow.id}-record-${index}`}
        estimate={180}
      >
        {(index) => {
          const record = flow.records[index];
          return (
            <article className="job" key={record.kind}>
              <div className="job-top">
                <span className="job-kind">
                  <strong>
                    <TextWindow
                      text={record.label}
                      stateKey={`${flow.id}-record-${index}-label`}
                    />
                  </strong>
                  <span className="mono">{labelPreview(record.kind)}</span>
                </span>
                <span className="badge">{labelPreview(record.status)}</span>
              </div>
              <p>
                <TextWindow
                  text={record.note}
                  stateKey={`${flow.id}-record-${index}-note`}
                />
              </p>
              <p className="mono">{labelPreview(flow.worker ?? "")}</p>
            </article>
          );
        }}
      </WindowList>
    </>
  );
}

export function DetailHeader({
  flow,
  tab,
  navOpen,
  onTab,
  onNav,
  onTabKeyDown,
  onSampleLink,
}: {
  flow: Flow;
  tab: Tab;
  navOpen: boolean;
  onTab: (tab: Tab) => void;
  onNav: () => void;
  onTabKeyDown: KeyboardEventHandler;
  onSampleLink: () => void;
}) {
  return (
    <header className="detail-head">
      <Button
        className="btn mobile-nav"
        data-action="nav"
        aria-expanded={navOpen}
        onClick={onNav}
      >
        <Menu />
        切换任务
      </Button>
      <div className="detail-title-row">
        <h1 tabIndex={-1} id="detail-heading">
          <TextWindow
            text={flow.title}
            stateKey={`${flow.id}-title`}
            maxChars={120}
          />
        </h1>
        <span className={`badge detail-activity phase-${flow.phase}`}>
          <i className="dot" />
          {labelPreview(flow.action, 40)}
        </span>
      </div>
      <div className="links">
        <span className="muted">b1indsight / {labelPreview(flow.repo)}</span>
        <Button
          className="btn ghost sample-link"
          data-action="sample-link"
          onClick={onSampleLink}
        >
          Issue #{flow.issue} ↗
        </Button>
        {flow.pr && (
          <Button
            className="btn ghost sample-link"
            data-action="sample-link"
            onClick={onSampleLink}
          >
            PR #{flow.pr} ↗
          </Button>
        )}
        <span className="muted">更新于{flow.age}</span>
        <span className="tag">只读 · 示例数据</span>
      </div>
      <div
        className="tabs"
        role="tablist"
        aria-label="Flow 详情"
        onKeyDown={onTabKeyDown}
      >
        {(
          [
            ["overview", "概览"],
            ["conversation", "会话"],
            ["jobs", "执行记录"],
          ] satisfies [Tab, string][]
        ).map(([key, label]) => (
          <button
            role="tab"
            id={`tab-${key}`}
            aria-controls="detail-panel"
            aria-selected={tab === key}
            tabIndex={tab === key ? 0 : -1}
            data-tab={key}
            onClick={() => onTab(key)}
            key={key}
          >
            {label}
            {key === "jobs" && (
              <>
                {" "}
                <span className="mono">{flow.records.length}</span>
              </>
            )}
          </button>
        ))}
      </div>
    </header>
  );
}
