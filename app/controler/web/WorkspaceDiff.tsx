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
  const [counts, setCounts] = useState<Counts | null>(null);
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

  if (!counts) return null;
  const total = counts.added + counts.deleted + counts.modified;
  if (total === 0) return null;
  const label = `工作区相对起点：新增 ${counts.added} 行，删除 ${counts.deleted} 行，修改 ${counts.modified} 行`;
  const values = [counts.added, counts.deleted, counts.modified];
  const colors = ["added", "deleted", "modified"];
  const segments = values.filter((value) => value > 0).length;
  const gap = segments > 1 ? 5 : 0;
  const coloredLength = 100 - segments * gap;
  let offset = gap / 2;
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
        {values.map((value, i) => {
          if (!value) return null;
          const start = offset;
          const length = (value / total) * coloredLength;
          offset += length + gap;
          return (
            <circle
              key={i}
              className={colors[i]}
              cx="10"
              cy="10"
              r="7.5"
              pathLength="100"
              stroke="currentColor"
              transform="rotate(-90 10 10)"
              strokeDasharray={`${length} ${100 - length}`}
              strokeDashoffset={-start}
            />
          );
        })}
      </svg>
      {values.map((value, i) => (
        <span key={i} className={colors[i]}>
          {["+", "−", "~"][i]}
          {compact.format(value).toLowerCase()}
        </span>
      ))}
    </span>
  );
}
