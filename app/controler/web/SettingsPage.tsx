import { useEffect, useId, useRef, useState } from "react";
import { ArrowLeft, Check, RefreshCw, Save } from "lucide-react";
import { ApiError, getSettings, updateSettings } from "./api";
import { Button } from "./components/ui/button";
import {
  ampModels,
  codexModels,
  codexEfforts,
  type ModelPreset,
} from "./modelPresets";
import type { CodingModelSettings } from "./types";

type Values = Omit<CodingModelSettings, "revision">;
type Draft = Record<keyof Values, string>;
type Agent = "codex" | "amp";
type WriteState =
  | { kind: "idle" | "saving" }
  | { kind: "notice" | "error" | "conflict"; message: string }
  | { kind: "unknown"; submitted: Values; revision: number; message: string };

function draftFrom(saved: Values): Draft {
  return {
    codex_model: saved.codex_model ?? "",
    amp_model: saved.amp_model ?? "",
    codex_effort:
      saved.codex_effort ?? defaultEffort("codex", saved.codex_model ?? ""),
    amp_effort: saved.amp_effort ?? defaultEffort("amp", saved.amp_model ?? ""),
  };
}

const valuesFrom = (draft: Draft): Values => ({
  codex_model: draft.codex_model || null,
  amp_model: draft.amp_model || null,
  codex_effort: draft.codex_effort || null,
  amp_effort: draft.amp_effort || null,
});
const sameValues = (a: Values, b: Values) =>
  a.codex_model === b.codex_model &&
  a.amp_model === b.amp_model &&
  a.codex_effort === b.codex_effort &&
  a.amp_effort === b.amp_effort;

function effortsFor(agent: Agent, model: string): readonly string[] {
  const presets = agent === "codex" ? codexModels : ampModels;
  return (
    presets.find((preset) => preset.id === model)?.efforts ??
    (agent === "codex" ? codexEfforts : [])
  );
}

function defaultEffort(agent: Agent, model: string): string {
  return effortsFor(agent, model).includes("medium") ? "medium" : "";
}

/** Keep both the draft and latest server value selectable when their catalogs differ. */
function PresetOptions({
  presets,
  current,
}: {
  presets: readonly Pick<ModelPreset, "id" | "displayName">[];
  current: (string | null | undefined)[];
}) {
  const noncatalog = [...new Set(current)].filter(
    (value): value is string =>
      Boolean(value) && !presets.some((model) => model.id === value),
  );
  return (
    <>
      {noncatalog.map((value) => (
        <option key={value} value={value}>
          当前值（不在预设中）：{value}
        </option>
      ))}
      {presets.map((model) => (
        <option key={model.id} value={model.id}>
          {model.displayName}
        </option>
      ))}
    </>
  );
}

function failureMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401)
    return "登录已失效，请重新登录后读取设置。";
  if (error instanceof ApiError && error.status === 403)
    return "没有操作权限，或请求未通过同源校验。";
  return error instanceof Error ? error.message : "请求失败";
}

/** Kept mounted by App so drafts and uncertain-write guards survive hash navigation. */
export function SettingsPage({
  active,
  refresh,
  onBack,
}: {
  active: boolean;
  refresh: number;
  onBack: () => void;
}) {
  const id = useId();
  const heading = useRef<HTMLHeadingElement>(null);
  const request = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const edits = useRef(0);
  const [saved, setSaved] = useState<CodingModelSettings | null>(null);
  const [draft, setDraft] = useState<Draft>({
    codex_model: "",
    amp_model: "",
    codex_effort: "medium",
    amp_effort: "",
  });
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState("");
  const [write, setWrite] = useState<WriteState>({ kind: "idle" });
  const [showServer, setShowServer] = useState(false);
  const values = valuesFrom(draft);
  const dirty = saved !== null && !sameValues(values, saved);
  const pending = write.kind === "saving";
  const locked = write.kind === "unknown" || write.kind === "conflict";
  const missingEffort = (["codex", "amp"] as const).some(
    (agent) =>
      effortsFor(agent, draft[`${agent}_model`]).length > 0 &&
      !draft[`${agent}_effort`],
  );

  const read = async () => {
    if (submitting.current) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const version = edits.current;
    const preserve = dirty || locked;
    setLoading(true);
    setReadError("");
    try {
      const current = await getSettings(controller.signal);
      if (controller.signal.aborted || request.current !== controller) return;
      setSaved(current);
      // A refresh may overlap typing; only a pristine, unchanged draft can be replaced.
      if (!preserve && version === edits.current) {
        setDraft(draftFrom(current));
        setShowServer(false);
      } else setShowServer(true);
      if (write.kind === "unknown") {
        setWrite({
          kind: "notice",
          message:
            current.revision > write.revision &&
            sameValues(current, write.submitted)
              ? "已回读核对：服务器设置与上次提交一致。草稿已保留。"
              : "已回读核对：服务器当前设置与上次提交不同。草稿已保留，请核对后再保存。",
        });
      } else if (write.kind === "conflict") {
        setWrite({
          kind: "notice",
          message: "已重新读取设置，草稿已保留。请核对服务器当前设置后再保存。",
        });
      }
    } catch (error) {
      if (!controller.signal.aborted && request.current === controller)
        setReadError(
          `设置读取失败：${failureMessage(error)}。草稿不会被清空。`,
        );
    } finally {
      if (request.current === controller) {
        request.current = null;
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    if (active) void read();
    return () => {
      request.current?.abort();
      request.current = null;
    };
  }, [active, refresh]);
  useEffect(() => {
    if (active) heading.current?.focus();
  }, [active]);

  const change = (next: Draft) => {
    edits.current++;
    setDraft(next);
    if (write.kind === "notice" || write.kind === "error")
      setWrite({ kind: "idle" });
  };
  const changeModel = (agent: Agent, next: string) => {
    const modelField = `${agent}_model` as const;
    const effortField = `${agent}_effort` as const;
    if (next === draft[modelField]) return;
    change({
      ...draft,
      [modelField]: next,
      [effortField]: effortsFor(agent, next).includes(draft[effortField])
        ? draft[effortField]
        : defaultEffort(agent, next),
    });
  };
  const submit = async () => {
    if (submitting.current || locked || !saved || !dirty || missingEffort)
      return;
    submitting.current = true;
    request.current?.abort();
    request.current = null;
    setLoading(false);
    setReadError("");
    setShowServer(false);
    setWrite({ kind: "saving" });
    try {
      const current = await updateSettings({
        ...values,
        expected_revision: saved.revision,
      });
      setSaved(current);
      setDraft(draftFrom(current));
      setShowServer(false);
      setWrite({ kind: "notice", message: "设置已保存，仅用于新建编码会话。" });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409)
        setWrite({
          kind: "conflict",
          message:
            "设置冲突：其他页面已修改设置。请先重新读取并核对；当前草稿已保留。",
        });
      else if (
        error instanceof ApiError &&
        [400, 401, 403, 422].includes(error.status)
      )
        setWrite({
          kind: "error",
          message:
            error.status === 400 || error.status === 422
              ? `校验失败：服务器未保存，请检查模型与推理强度（${error.message}）。`
              : failureMessage(error),
        });
      else
        setWrite({
          kind: "unknown",
          submitted: values,
          revision: saved.revision,
          message:
            "保存结果未知：请先回读核对服务器设置。草稿已保留，不会自动重发。",
        });
    } finally {
      submitting.current = false;
    }
  };

  if (!active) return null;
  return (
    <main className="settings-page" aria-label="设置">
      <div className="settings-content">
        <div className="settings-heading">
          <Button
            type="button"
            className="btn"
            aria-label="返回看板"
            title="返回看板"
            onClick={onBack}
          >
            <ArrowLeft aria-hidden="true" />
          </Button>
          <h1 ref={heading} tabIndex={-1}>
            设置
          </h1>
        </div>
        <section aria-labelledby={`${id}-models`}>
          <h2 id={`${id}-models`}>Code agent 模型</h2>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
            aria-busy={pending}
          >
            {(["codex", "amp"] as const).map((agent) => {
              const label = agent === "codex" ? "Codex" : "Amp";
              const modelField = `${agent}_model` as const;
              const effortField = `${agent}_effort` as const;
              const efforts = effortsFor(agent, draft[modelField]);
              const currentEffort =
                saved?.[modelField] === draft[modelField]
                  ? saved[effortField]
                  : undefined;
              const hasEfforts =
                efforts.length > 0 || !!draft[effortField] || !!currentEffort;
              return (
                <div className="settings-agent" key={agent}>
                  <div className="settings-field">
                    <label htmlFor={`${id}-${agent}`}>{label} 模型</label>
                    <select
                      id={`${id}-${agent}`}
                      value={draft[modelField]}
                      disabled={!saved || pending}
                      onChange={(event) =>
                        changeModel(agent, event.target.value)
                      }
                    >
                      <option value="">
                        {agent === "codex"
                          ? "不指定（原生默认）"
                          : "不指定（默认 low）"}
                      </option>
                      <PresetOptions
                        presets={agent === "codex" ? codexModels : ampModels}
                        current={[draft[modelField], saved?.[modelField]]}
                      />
                    </select>
                  </div>
                  <div className="settings-field">
                    <select
                      id={`${id}-${agent}-effort`}
                      aria-label={`${label} 推理强度`}
                      value={draft[effortField]}
                      disabled={!saved || pending || !hasEfforts}
                      onChange={(event) =>
                        change({ ...draft, [effortField]: event.target.value })
                      }
                    >
                      {!draft[effortField] && (
                        <option value="" disabled>
                          {hasEfforts ? "选择强度" : "—"}
                        </option>
                      )}
                      <PresetOptions
                        presets={efforts.map((level) => ({
                          id: level,
                          displayName: level === "medium" ? "mid" : level,
                        }))}
                        current={[draft[effortField], currentEffort]}
                      />
                    </select>
                  </div>
                </div>
              );
            })}
            <div className="settings-feedback">
              <p role="status" aria-live="polite">
                {pending
                  ? "正在保存，请勿重复提交…"
                  : loading
                    ? "正在读取设置…"
                    : write.kind === "notice"
                      ? write.message
                      : dirty
                        ? "未保存修改"
                        : ""}
              </p>
              {write.kind === "notice" && dirty && <p>未保存修改</p>}
              {readError && (
                <p className="settings-error" role="alert">
                  {readError}
                </p>
              )}
              {(write.kind === "error" ||
                write.kind === "conflict" ||
                write.kind === "unknown") && (
                <p className="settings-error" role="alert">
                  {write.message}
                </p>
              )}
            </div>
            {showServer && saved && (
              <section className="settings-server" aria-label="服务器当前设置">
                <h3>服务器当前设置 · 版本 {saved.revision}</h3>
                <dl>
                  <dt>Codex</dt>
                  <dd>
                    {saved.codex_model ?? "不指定（原生默认）"} /{" "}
                    {saved.codex_effort === "medium"
                      ? "mid"
                      : (saved.codex_effort ?? "—")}
                  </dd>
                  <dt>Amp</dt>
                  <dd>
                    {saved.amp_model ?? "不指定（默认 low）"} /{" "}
                    {saved.amp_effort === "medium"
                      ? "mid"
                      : (saved.amp_effort ?? "—")}
                  </dd>
                </dl>
                <Button
                  type="button"
                  className="btn"
                  disabled={pending || loading || locked}
                  onClick={() => {
                    edits.current++;
                    setDraft(draftFrom(saved));
                    setShowServer(false);
                    setWrite({ kind: "idle" });
                  }}
                >
                  <Check aria-hidden="true" />
                  使用服务器设置
                </Button>
              </section>
            )}
            <div className="settings-actions">
              <Button
                type="button"
                className="btn"
                disabled={loading || pending}
                onClick={() => void read()}
              >
                <RefreshCw aria-hidden="true" />
                {write.kind === "unknown" ? "回读核对" : "重新读取设置"}
              </Button>
              <Button
                type="submit"
                className="btn settings-save"
                disabled={
                  !saved ||
                  !dirty ||
                  pending ||
                  loading ||
                  locked ||
                  missingEffort
                }
              >
                <Save aria-hidden="true" />
                {pending ? "正在保存…" : "保存设置"}
              </Button>
            </div>
          </form>
        </section>
      </div>
    </main>
  );
}
