import type { SourceStatus } from "./types";

// These are upstream history limits, not failures that another sync can repair.
const historyLimits = new Set([
  "remote_deleted_or_pre_ingestion_versions_unrecoverable",
  "pre_ingestion_edits_and_deleted_bodies_unrecoverable",
  "reaction_removals_have_no_webhook_or_revision_clock",
  "expired_remote_logs_unrecoverable",
  "pending_review_timestamp_unavailable",
  "source_event_timestamp_unavailable",
]);
const statusLabels: Record<SourceStatus["status"], string> = {
  pending: "等待同步",
  syncing: "同步中",
  available: "已同步",
  partial: "完整性受限",
  unavailable: "读取失败",
};
const reasonLabels: Record<string, string> = {
  authentication: "认证失败",
  permission: "访问权限不足",
  rate_limited: "请求受限，等待重试",
  transient: "远端服务或网络暂不可用",
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
                  `${source.source === "worker" ? "执行会话" : source.source}：${source.reason ? (reasonLabels[source.reason] ?? "同步异常") : "内容采集不完整"}`,
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
        <p className="source-details-hint">
          完整性受限不一定表示同步失败；部分远端历史无法还原。时间为上次成功同步时间。
        </p>
        <ul>
          {sources.map((source) => (
            <li key={source.source}>
              <span
                className={`source-state ${isEmptySession(source) ? "empty" : source.status}`}
              >
                <i className="dot" aria-hidden="true" />
                {source.source} ·{" "}
                {isEmptySession(source)
                  ? "暂无会话"
                  : statusLabels[source.status]}
              </span>
              {source.reason && !isEmptySession(source) && (
                <span>{reasonLabels[source.reason] ?? source.reason}</span>
              )}
              {source.last_success_at && (
                <time dateTime={source.last_success_at}>
                  {new Date(source.last_success_at).toLocaleString()}
                </time>
              )}
              {source.limitations.length > 0 && (
                <ul className="source-limitations">
                  {source.limitations.map((limit) => (
                    <li key={limit}>{limit}</li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}
