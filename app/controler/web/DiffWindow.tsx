import { type ReactNode, useMemo } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import {
  Decoration,
  Diff,
  type FileData,
  Hunk,
  markEdits,
  parseDiff,
  tokenize,
} from "react-diff-view";
import { CODE_LINES, useReadingState } from "./windowing";

/** Parse a patch or standalone GitHub hunk; window rows without losing their original line numbers. */
export function DiffWindow({
  text,
  stateKey,
  fallback = <p>没有文本差异。</p>,
}: {
  text: string;
  stateKey: string;
  fallback?: ReactNode;
}) {
  const files = useMemo(() => {
    try {
      // GitHub's diffHunk omits the file headers required by the patch parser.
      return parseDiff(
        text.startsWith("@@ ") ? `--- a/code\n+++ b/code\n${text}` : text,
      );
    } catch {
      // A fenced diff can be an incomplete patch or just an illustrative snippet.
      return [];
    }
  }, [text]);
  if (
    !files.length ||
    files.some((file) => !file.hunks.some((hunk) => hunk.changes.length))
  )
    return fallback;
  return files.map((file, index) => (
    <FileDiffWindow key={index} file={file} stateKey={`${stateKey}-${index}`} />
  ));
}

function FileDiffWindow({
  file,
  stateKey,
}: {
  file: FileData;
  stateKey: string;
}) {
  const tokens = useMemo(
    () => tokenize(file.hunks, { enhancers: [markEdits(file.hunks)] }),
    [file],
  );
  const [saved, setStart] = useReadingState(stateKey, 0);
  const total = file.hunks.reduce((n, hunk) => n + hunk.changes.length, 0);
  const start = saved < total ? saved : 0;
  const end = Math.min(total, start + CODE_LINES);
  let skipped = 0;
  const hunks = file.hunks.flatMap((hunk) => {
    const from = Math.max(0, start - skipped);
    const to = Math.min(hunk.changes.length, end - skipped);
    skipped += hunk.changes.length;
    if (from >= to) return [];
    return [{ ...hunk, changes: hunk.changes.slice(from, to) }];
  });
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
            aria-label="上一段差异"
            title="上一段差异"
            disabled={!start}
            onClick={() => setStart(Math.max(0, start - CODE_LINES))}
          >
            <ArrowLeft aria-hidden="true" />
          </button>
          <span>
            差异行 {start + 1}–{end} / {total}
          </span>
          <button
            type="button"
            aria-label="下一段差异"
            title="下一段差异"
            disabled={end === total}
            onClick={() => setStart(end)}
          >
            <ArrowRight aria-hidden="true" />
          </button>
        </div>
      )}
    </>
  );
}
