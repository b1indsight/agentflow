import { useEffect, useState } from "react";
import { api } from "./api";

type Counts = { added: number; deleted: number; modified: number };
const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});

export function WorkspaceDiff({
  id,
  refresh,
}: {
  id: number;
  refresh: number;
}) {
  const [counts, setCounts] = useState<Counts | null | undefined>();
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      if (!document.hidden) {
        const next = await api<Counts | null>(
          `/api/flows/${id}/workspace-diff`,
          {},
          controller.signal,
        ).catch(() => null);
        if (controller.signal.aborted) return;
        setCounts(next);
      }
      timer = window.setTimeout(load, 15000);
    }
    void load();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [id, refresh]);

  if (counts === undefined) return null;
  const total = counts ? counts.added + counts.deleted + counts.modified : 0;
  const label = counts
    ? `工作区相对起点：新增 ${counts.added} 行，删除 ${counts.deleted} 行，修改 ${counts.modified} 行`
    : "工作区差异暂不可用";
  const values = counts ? [counts.added, counts.deleted, counts.modified] : [];
  const colors = ["added", "deleted", "modified"];
  let offset = 0;
  return (
    <span
      className="workspace-diff"
      role="img"
      aria-label={label}
      title={label}
    >
      <svg
        aria-hidden="true"
        width="18"
        height="18"
        viewBox="0 0 20 20"
        fill="none"
        strokeWidth="3"
      >
        <circle cx="10" cy="10" r="7.5" stroke="var(--line)" />
        {total > 0 &&
          values.map((value, i) => {
            const start = offset;
            const fraction = (value / total) * 100;
            offset += fraction;
            return (
              value > 0 && (
                <circle
                  key={i}
                  className={colors[i]}
                  cx="10"
                  cy="10"
                  r="7.5"
                  pathLength="100"
                  stroke="currentColor"
                  transform="rotate(-90 10 10)"
                  strokeDasharray={`${fraction} ${100 - fraction}`}
                  strokeDashoffset={-start}
                />
              )
            );
          })}
      </svg>
      {counts
        ? values.map((value, i) => (
            <span key={i} className={colors[i]}>
              {["+", "−", "~"][i]}
              {compact.format(value).toLowerCase()}
            </span>
          ))
        : "差异暂不可用"}
    </span>
  );
}
