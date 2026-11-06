import { useEffect, useRef, useState } from "react";
import { ApiError, getInferenceNotices } from "./api";
import type { InferenceNotice, InferenceNoticePage } from "./types";
import { WindowList } from "./windowing";

const failureLabels: Record<InferenceNotice["last_failure_code"], string> = {
  unavailable: "暂无可用的推理 Worker",
  busy: "推理 Worker 忙碌",
  connection_closed: "推理连接已断开",
  rpc_timeout: "推理请求超时",
  execution_timeout: "推理执行超时",
  rate_limited: "推理服务限流",
  backend_unavailable: "推理后端暂不可用",
  authentication: "身份验证失败",
  configuration: "推理配置错误",
  invalid_response: "推理响应无效",
  output_limit: "推理输出超限",
  backend_error: "推理后端错误",
};
const stopLabels: Record<InferenceNotice["stop_reason"], string> = {
  max_attempts: "已达尝试上限",
  deadline: "总预算耗尽",
  non_retryable: "不可直接重试",
  unavailable: "无可用推理 Worker，停止本轮尝试",
};

/** Mount only in the jobs tab; the parent keys by Flow and refresh to reset the snapshot. */
export function InferenceNotices({ id }: { id: number }) {
  const [page, setPage] = useState<InferenceNoticePage | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const request = useRef<AbortController | null>(null);
  const read = () => {
    if (request.current || (page && !page.has_more)) return;
    const controller = new AbortController();
    request.current = controller;
    setBusy(true);
    setError("");
    void getInferenceNotices(
      id,
      page ? { cursor: page.next_cursor, snapshot: page.snapshot } : {},
      controller.signal,
    )
      .then((next) => {
        if (controller.signal.aborted) return;
        if (page && next.snapshot !== page.snapshot) {
          setError("推理通知快照已变化，请刷新当前视图后再读取。");
          return;
        }
        setPage({ ...next, items: [...(page?.items ?? []), ...next.items] });
      })
      .catch((failure) => {
        if (controller.signal.aborted) return;
        // Proxy and backend error bodies may contain diagnostics; expose only safe read guidance.
        setError(
          failure instanceof ApiError && failure.status === 401
            ? "推理通知读取失败：登录已失效，请重新登录。"
            : failure instanceof ApiError && failure.status === 403
              ? "推理通知读取失败：没有读取权限。"
              : "推理通知读取失败，已有记录保留，请稍后重新读取。",
        );
      })
      .finally(() => {
        if (request.current !== controller || controller.signal.aborted) return;
        request.current = null;
        setBusy(false);
      });
  };
  useEffect(() => {
    read();
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, []);

  const items = page?.items ?? [];
  return (
    <section
      className="inference-notices"
      aria-labelledby="inference-notices-heading"
    >
      <h2 className="panel-title" id="inference-notices-heading">
        推理通知
      </h2>
      <p className="panel-note">记录未成功的推理轮次，不代表整个 Flow 失败。</p>
      <WindowList
        count={items.length}
        label="推理通知"
        itemKey={(i) => items[i].key}
        estimate={290}
      >
        {(i) => {
          const notice = items[i];
          return (
            <article
              className="inference-notice"
              data-historical={notice.historical}
            >
              <div className="inference-notice-top">
                <h3>本轮推理未成功</h3>
                {notice.historical && <span className="badge">历史通知</span>}
              </div>
              <time dateTime={notice.occurred_at}>
                发生于 {new Date(notice.occurred_at).toLocaleString()}
              </time>
              <p className="inference-attempts">
                尝试 {notice.attempts} / {notice.max_attempts} 次 ·{" "}
                {stopLabels[notice.stop_reason]}
              </p>
              <dl>
                <dt>最后失败</dt>
                <dd>{failureLabels[notice.last_failure_code]}</dd>
                {notice.last_worker_id !== null && (
                  <>
                    <dt>Worker</dt>
                    <dd>{notice.last_worker_id}</dd>
                  </>
                )}
                {notice.backend !== null && (
                  <>
                    <dt>后端</dt>
                    <dd>{notice.backend}</dd>
                  </>
                )}
                <dt>耗时</dt>
                <dd>{notice.elapsed_ms / 1000} 秒</dd>
              </dl>
              <div className="next-step">
                <span>后续处理 · 当前状态说明</span>
                <p>{notice.next_condition}</p>
              </div>
            </article>
          );
        }}
      </WindowList>
      {busy && (
        <p className="panel-note" role="status">
          正在读取推理通知…
        </p>
      )}
      {!busy && !error && !items.length && (
        <p className="panel-note">暂无推理通知</p>
      )}
      {error && <p role="alert">{error}</p>}
      {error ? (
        <button className="btn" disabled={busy} onClick={read}>
          重新读取推理通知
        </button>
      ) : (
        page?.has_more && (
          <button className="btn" disabled={busy} onClick={read}>
            读取更多推理通知
          </button>
        )
      )}
    </section>
  );
}
