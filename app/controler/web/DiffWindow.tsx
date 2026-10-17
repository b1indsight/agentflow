import { useMemo } from "react";
import {
  Decoration,
  Diff,
  Hunk,
  markEdits,
  parseDiff,
  tokenize,
} from "react-diff-view";
import { CODE_LINES, useReadingState } from "./windowing";

/** Parse a complete patch once; window parsed rows without breaking patch syntax or line numbers. */
export function DiffWindow({
  text,
  stateKey,
}: {
  text: string;
  stateKey: string;
}) {
  const file = useMemo(() => parseDiff(text)[0], [text]);
  const tokens = useMemo(
    () => file && tokenize(file.hunks, { enhancers: [markEdits(file.hunks)] }),
    [file],
  );
  const [saved, setStart] = useReadingState(stateKey, 0);
  const total =
    file?.hunks.reduce((n, hunk) => n + hunk.changes.length, 0) ?? 0;
  const start = saved < total ? saved : 0;
  const end = Math.min(total, start + CODE_LINES);
  let skipped = 0;
  const hunks = file?.hunks.flatMap((hunk) => {
    const from = Math.max(0, start - skipped);
    const to = Math.min(hunk.changes.length, end - skipped);
    skipped += hunk.changes.length;
    if (from >= to) return [];
    return [{ ...hunk, changes: hunk.changes.slice(from, to) }];
  });
  if (!file || !hunks || !total) return <p>没有文本差异。</p>;
  return (
    <>
      <div
        key={start}
        className="diff-window"
        tabIndex={0}
        aria-label="版本差异"
      >
        <Diff
          viewType="unified"
          diffType={file.type}
          hunks={hunks}
          tokens={tokens}
        >
          {(visible) =>
            visible.flatMap((hunk) => [
              <Decoration key={`header-${hunk.content}`}>
                {hunk.content}
              </Decoration>,
              <Hunk key={hunk.content} hunk={hunk} />,
            ])
          }
        </Diff>
      </div>
      {total > CODE_LINES && (
        <div
          className="text-window-controls"
          role="group"
          aria-label="版本差异分段浏览"
        >
          <button
            type="button"
            disabled={!start}
            onClick={() => setStart(Math.max(0, start - CODE_LINES))}
          >
            上一段差异
          </button>
          <span>
            差异行 {start + 1}–{end} / {total}
          </span>
          <button
            type="button"
            disabled={end === total}
            onClick={() => setStart(end)}
          >
            下一段差异
          </button>
        </div>
      )}
    </>
  );
}
