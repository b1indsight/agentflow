import type { SourceStatus } from "./types";

// These are upstream history limits, not failures that another sync can repair.
const historyLimits = new Set([
  "remote_deleted_or_pre_ingestion_versions_unrecoverable",
  "pre_ingestion_edits_and_deleted_bodies_unrecoverable",
  "reaction_removals_have_no_webhook_or_revision_clock",
  "expired_remote_logs_unrecoverable",
  "pending_review_timestamp_unavailable",
  "source_event_timestamp_unavailable",
  "github_pr_commit_list_maximum_250",
  "thread_state_has_no_remote_change_timestamp",
]);
const statusLabels: Record<SourceStatus["status"], string> = {
  pending: "等待同步",
  syncing: "同步中",
  available: "已同步",
  partial: "采集不完整",
  unavailable: "读取失败",
};
const sourceLabels: Record<string, string> = {
  github: "GitHub 同步入口",
  "issue.description": "Issue 正文",
  "issue.comments": "Issue 评论",
  "issue.timeline": "Issue 时间线",
  "issue.reactions": "Issue 表情回应",
  "pr.description": "PR 正文",
  "pr.comments": "PR 评论",
  "pr.timeline": "PR 时间线",
  "pr.reactions": "PR 表情回应",
  "pr.reviews": "PR 评审",
  "pr.review_threads": "PR 评审讨论",
  "pr.review_thread_replies": "PR 评审回复",
  "pr.commits": "PR 提交记录",
  "pr.check_runs": "PR 检查结果",
  "pr.statuses": "PR 提交状态",
  "comment.reactions": "评论表情回应",
  "inline.reactions": "行内评论表情回应",
  "review.reactions": "评审表情回应",
  "commit.check_runs": "提交检查结果",
  "commit.statuses": "提交状态",
  worker: "Worker 执行会话",
};
const limitationLabels: Record<string, string> = {
  remote_deleted_or_pre_ingestion_versions_unrecoverable:
    "无法恢复远端已删除的内容或首次采集前的历史版本。",
  pre_ingestion_edits_and_deleted_bodies_unrecoverable:
    "无法恢复首次采集前的正文编辑历史和已删除的正文。",
  reaction_removals_have_no_webhook_or_revision_clock:
    "无法完整追踪表情回应的移除记录。",
  expired_remote_logs_unrecoverable: "远端已过期的日志无法恢复。",
  pending_review_timestamp_unavailable: "未提交的评审缺少时间信息，暂未收录。",
  source_event_timestamp_unavailable: "缺少时间信息的来源记录暂未收录。",
  github_pr_commit_list_maximum_250: "GitHub 最多返回此 PR 的 250 条提交记录。",
  thread_state_has_no_remote_change_timestamp:
    "评审讨论状态缺少远端变更时间，无法精确还原变更历史。",
  worker_source_truncated: "部分执行会话内容已在来源端截断。",
};
const reasonLabels: Record<string, string> = {
  no_session: "尚无执行会话",
  no_worker: "尚未绑定 Worker",
  no_installation: "尚未关联 GitHub App 安装",
  authentication: "认证失败",
  permission: "访问权限不足",
  rate_limited: "请求受限，等待重试",
  transient: "远端服务或网络暂不可用",
  unexpected_response: "来源返回了无法识别的数据",
  worker_unavailable: "Worker 暂不可连接",
  query_timeout: "会话读取超时",
  sync_timeout: "同步超时",
  history_unreadable: "会话历史无法读取",
  not_found: "来源内容未找到",
  unsupported: "会话格式暂不支持",
};

function isEmptySession(source: SourceStatus) {
  return (
    source.source === "worker" &&
    source.status === "unavailable" &&
    (source.reason === "no_session" || source.reason === "no_worker")
  );
}

function needsAttention(source: SourceStatus) {
  if (isEmptySession(source)) return false;
  if (source.status === "unavailable") return true;
  return (
    source.status === "partial" &&
    (source.reason !== null ||
      source.limitations.length === 0 ||
      source.limitations.some((limit) => !historyLimits.has(limit)))
  );
}

function sourceLabel(source: string) {
  const family = source.split(":")[0];
  return sourceLabels[family] ?? source;
}

/** Shared source details for the overview and the expandable conversation diagnostics. */
export function SourceList({ sources }: { sources: SourceStatus[] }) {
  if (!sources.length) return null;
  return (
    <>
      <p className="source-details-hint">
        同步状态表示当前进度，采集限制单独列出。历史记录受限不代表本次同步失败。时间按本地时区显示。
      </p>
      <ul className="source-list" aria-label="数据来源明细">
        {sources.map((source) => {
          const empty = isEmptySession(source);
          const queued =
            source.status === "pending" || source.status === "syncing";
          const historyOnly =
            source.status === "partial" && !needsAttention(source);
          return (
            <li key={source.source}>
              <div className="source-heading">
                <strong title={source.source}>
                  {sourceLabel(source.source)}
                </strong>
                <span
                  className={`source-state ${empty ? "empty" : source.status}`}
                >
                  <i className="dot" aria-hidden="true" />
                  {empty
                    ? "暂无会话"
                    : historyOnly
                      ? "历史记录受限"
                      : statusLabels[source.status]}
                </span>
              </div>
              <p className="source-last-success">
                上次成功同步：
                {source.last_success_at ? (
                  <time dateTime={source.last_success_at}>
                    {new Date(source.last_success_at).toLocaleString("zh-CN", {
                      hour12: false,
                    })}
                  </time>
                ) : (
                  "暂无记录"
                )}
              </p>
              {source.reason && (
                <p className="source-reason">
                  {queued ? "上次读取：" : "读取情况："}
                  {reasonLabels[source.reason] ??
                    `同步异常（${source.reason}）`}
                </p>
              )}
              {source.limitations.length > 0 && (
                <div className="source-coverage">
                  <span>采集限制</span>
                  <ul className="source-limitations">
                    {source.limitations.map((limit) => (
                      <li key={limit}>
                        {limitationLabels[limit] ?? `其他采集限制（${limit}）`}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

export function ConversationSources({
  sources,
  busy,
  refresh,
}: {
  sources: SourceStatus[];
  busy: boolean;
  refresh: () => void;
}) {
  if (!sources.length) return null;
  const failures = sources.filter(needsAttention);
  const emptySession = sources.find(isEmptySession);
  const syncing = sources.some(
    (source) => source.status === "pending" || source.status === "syncing",
  );

  return (
    <section className="conversation-source-status" aria-label="会话数据状态">
      {failures.length > 0 && (
        <div className="conversation-source-warning" role="alert">
          <strong>部分会话内容暂不可用或不完整</strong>
          <p>
            {failures
              .map(
                (source) =>
                  `${sourceLabel(source.source)}：${source.reason ? (reasonLabels[source.reason] ?? "同步异常") : "内容采集不完整"}`,
              )
              .join("；")}
            。已读取的内容仍可查看。
          </p>
          <button type="button" disabled={busy} onClick={refresh}>
            {busy ? "正在检查更新…" : "重新同步"}
          </button>
        </div>
      )}
      {emptySession && (
        <p className="conversation-source-empty" role="status">
          {emptySession.reason === "no_session"
            ? "尚无执行会话"
            : "尚未绑定 Worker，暂不读取执行会话"}
        </p>
      )}
      <details className="conversation-source-details">
        <summary>数据来源与完整性{syncing ? "（同步中）" : ""}</summary>
        <SourceList sources={sources} />
      </details>
    </section>
  );
}
