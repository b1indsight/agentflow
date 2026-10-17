import { useEffect, useId, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { phases } from "./phases";
import { parseSearch } from "./search";
import { Input } from "./components/ui/input";
import { Button } from "./components/ui/button";

interface Suggestion {
  value: string;
  label: string;
  description: string;
}
const fields: Suggestion[] = [
  { value: "repo:", label: "仓库", description: "repo:owner/name" },
  { value: "is:", label: "生命周期", description: "is:active · is:done" },
  { value: "status:", label: "执行状态", description: "status:running" },
  { value: "issue:", label: "Issue 编号", description: "issue:82" },
  { value: "pr:", label: "PR 编号", description: "pr:95" },
];
const PAGE = 8;

/** One submitted query drives filters; suggestions and IME input remain local drafts. */
export function SearchBox({
  query,
  repos,
  onQuery,
}: {
  query: string;
  repos: readonly string[];
  onQuery: (value: string) => void;
}) {
  const [draft, setDraft] = useState(query),
    [open, setOpen] = useState(false),
    [active, setActive] = useState(-1),
    [page, setPage] = useState(0),
    [caret, setCaret] = useState(query.length),
    [error, setError] = useState("");
  const input = useRef<HTMLInputElement>(null),
    composing = useRef(false);
  const id = useId();
  useEffect(() => {
    setDraft(query);
    setCaret(query.length);
    const parsed = parseSearch(query, repos);
    setError(parsed.ok ? "" : parsed.error);
  }, [query]);
  const tokens = [...draft.matchAll(/(?:[^\s"]|"[^"]*")+/g)];
  const current = tokens.find(
    (token) => token.index <= caret && token.index + token[0].length >= caret,
  );
  const start = current?.index ?? caret,
    end = start + (current?.[0].length ?? 0);
  const token = current?.[0] ?? "";
  const colon = token.indexOf(":"),
    key = token.slice(0, colon).toLowerCase(),
    prefix = token.slice(colon + 1).toLowerCase();
  const candidates: Suggestion[] =
    colon < 0
      ? fields.filter((field) => field.value.startsWith(token.toLowerCase()))
      : key === "repo"
        ? repos
            .filter((repo) => repo.toLowerCase().includes(prefix))
            .map((repo) => ({
              value: `repo:${repo}`,
              label: repo,
              description: "仓库",
            }))
        : key === "is"
          ? [
              ["active", "活跃任务"],
              ["done", "已完成"],
            ]
              .filter(([value]) => value.startsWith(prefix))
              .map(([value, label]) => ({
                value: `is:${value}`,
                label,
                description: `is:${value}`,
              }))
          : key === "status"
            ? phases
                .filter(([value]) => value.startsWith(prefix))
                .map(([value, label]) => ({
                  value: `status:${value}`,
                  label,
                  description: `status:${value}`,
                }))
            : [];
  const pages = Math.max(1, Math.ceil(candidates.length / PAGE)),
    safePage = Math.min(page, pages - 1);
  const suggestions = candidates.slice(safePage * PAGE, (safePage + 1) * PAGE);
  const expanded = open && !composing.current && suggestions.length > 0;
  const insert = (suggestion: Suggestion) => {
    const value =
      suggestion.value + (suggestion.value.endsWith(":") ? "" : " ");
    const next = draft.slice(0, start) + value + draft.slice(end);
    const nextCaret = start + value.length;
    setDraft(next);
    setCaret(nextCaret);
    setActive(-1);
    setPage(0);
    setError("");
    setOpen(suggestion.value.endsWith(":"));
    requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.setSelectionRange(nextCaret, nextCaret);
    });
  };
  const submit = () => {
    const parsed = parseSearch(draft, repos);
    if (!parsed.ok) {
      setError(parsed.error);
      setOpen(false);
      return;
    }
    onQuery(draft.trim());
    setError("");
    setOpen(false);
    setActive(-1);
  };
  useEffect(() => {
    if (active >= 0)
      document
        .getElementById(`${id}-option-${active}`)
        ?.scrollIntoView({ block: "nearest" });
  }, [active, id]);
  return (
    <div
      className="unified-search"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setOpen(false);
          setActive(-1);
        }
      }}
    >
      <div className="query-field">
        <Search className="query-icon" aria-hidden="true" />
        <Input
          ref={input}
          type="search"
          role="combobox"
          aria-autocomplete="list"
          aria-label="搜索任务与筛选条件"
          aria-expanded={expanded}
          aria-controls={expanded ? `${id}-list` : undefined}
          aria-activedescendant={
            expanded && active >= 0 ? `${id}-option-${active}` : undefined
          }
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `${id}-error` : undefined}
          className="query-input"
          placeholder="搜索任务，或输入 repo:、status: 等筛选条件…"
          value={draft}
          onFocus={() => setOpen(true)}
          onSelect={(event) => {
            const next = event.currentTarget.selectionStart ?? draft.length;
            if (next !== caret) {
              setCaret(next);
              setActive(-1);
              setPage(0);
            }
          }}
          onCompositionStart={() => {
            composing.current = true;
            setOpen(false);
          }}
          onCompositionEnd={(event) => {
            composing.current = false;
            setDraft(event.currentTarget.value);
            setCaret(
              event.currentTarget.selectionStart ??
                event.currentTarget.value.length,
            );
            setOpen(true);
          }}
          onChange={(event) => {
            setDraft(event.target.value);
            setCaret(event.target.selectionStart ?? event.target.value.length);
            setOpen(true);
            setActive(-1);
            setPage(0);
            setError("");
          }}
          onKeyDown={(event) => {
            if (
              composing.current ||
              event.nativeEvent.isComposing ||
              event.keyCode === 229
            )
              return;
            if (event.key === "Escape") {
              if (open) {
                event.preventDefault();
                event.stopPropagation();
                setOpen(false);
                setActive(-1);
              }
              return;
            }
            if (
              (event.key === "ArrowDown" || event.key === "ArrowUp") &&
              suggestions.length
            ) {
              event.preventDefault();
              event.stopPropagation();
              setOpen(true);
              setActive((before) =>
                event.key === "ArrowDown"
                  ? (before + 1) % suggestions.length
                  : before <= 0
                    ? suggestions.length - 1
                    : before - 1,
              );
            } else if (event.key === "Enter") {
              event.preventDefault();
              event.stopPropagation();
              if (expanded && active >= 0 && suggestions[active])
                insert(suggestions[active]);
              else submit();
            }
          }}
        />
        {draft && (
          <Button
            className="query-clear"
            variant="ghost"
            aria-label="清空搜索条件"
            onClick={() => {
              setDraft("");
              setCaret(0);
              setError("");
              setPage(0);
              setActive(-1);
              onQuery("");
              input.current?.focus();
            }}
          >
            <X />
          </Button>
        )}
      </div>
      {expanded && (
        <div className="query-suggestions">
          <div id={`${id}-list`} role="listbox" aria-label="筛选建议">
            {suggestions.map((suggestion, index) => (
              <div
                key={suggestion.value}
                id={`${id}-option-${index}`}
                className="query-option"
                role="option"
                aria-selected={active === index}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => insert(suggestion)}
                onMouseMove={() => setActive(index)}
              >
                <span className="query-option-label">{suggestion.label}</span>
                <code className="query-option-description">
                  {suggestion.description}
                </code>
              </div>
            ))}
          </div>
          <div className="query-suggestion-footer">
            <span>↑ ↓ 选择 · Enter 补入 / 搜索 · Esc 关闭</span>
            {pages > 1 && (
              <span className="query-pages">
                <button
                  disabled={!safePage}
                  onClick={() => {
                    setPage(safePage - 1);
                    setActive(-1);
                  }}
                >
                  上一组
                </button>
                <span>
                  {safePage + 1} / {pages}
                </span>
                <button
                  disabled={safePage === pages - 1}
                  onClick={() => {
                    setPage(safePage + 1);
                    setActive(-1);
                  }}
                >
                  下一组
                </button>
              </span>
            )}
          </div>
        </div>
      )}
      {error && (
        <div className="query-error" id={`${id}-error`} role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
