import { useEffect, useId, useRef, useState } from "react";
import { ArrowLeft, Check, RefreshCw } from "lucide-react";
import { ApiError, getModelCatalog, getSettings, updateSettings } from "./api";
import { Button } from "./components/ui/button";
import { FontSettings } from "./FontSettings";
import { SettingActions, SettingSelect } from "./SettingControls";
import { SkillRepositorySetting } from "./SkillRepositorySetting";
import type { CodingModelSettings, ModelCatalog } from "./types";

type Values = Omit<CodingModelSettings, "revision">;
type Draft = Record<keyof Values, string>;
type Agent = "codex" | "amp";
type WriteState =
  | { kind: "idle" | "saving" }
  | { kind: "notice" | "error" | "conflict"; message: string }
  | { kind: "unknown"; submitted: Values; revision: number; message: string };

function draftFrom(saved: Values, catalog: ModelCatalog | null): Draft {
  return {
    codex_model: saved.codex_model ?? "",
    amp_model: saved.amp_model ?? "",
    codex_effort:
      saved.codex_effort ??
      defaultEffort(catalog, "codex", saved.codex_model ?? ""),
    amp_effort:
      saved.amp_effort ?? defaultEffort(catalog, "amp", saved.amp_model ?? ""),
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

function effortsFor(
  catalog: ModelCatalog | null,
  agent: Agent,
  model: string,
): string[] {
  const models = catalog?.[agent].models ?? [];
  const known = models.find((entry) => entry.id === model);
  if (known) return known.efforts;
  if (agent === "amp") return [];
  return [...new Set(models.flatMap((entry) => entry.efforts))];
}

function defaultEffort(
  catalog: ModelCatalog | null,
  agent: Agent,
  model: string,
): string {
  if (agent === "codex" && !model) return "medium";
  return effortsFor(catalog, agent, model).includes("medium") ? "medium" : "";
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
  const catalogRequest = useRef<AbortController | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
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
      effortsFor(catalog, agent, draft[`${agent}_model`]).length > 0 &&
      !draft[`${agent}_effort`],
  );

  const readCatalog = async () => {
    catalogRequest.current?.abort();
    const controller = new AbortController();
    catalogRequest.current = controller;
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const current = await getModelCatalog(controller.signal);
      if (!controller.signal.aborted) setCatalog(current);
      return current;
    } catch (error) {
      if (!controller.signal.aborted) {
        setCatalog(null);
        setCatalogError(
          error instanceof TypeError
            ? "模型目录读取失败，请检查网络后刷新目录。"
            : `模型目录读取失败：${failureMessage(error)}。可刷新目录重试。`,
        );
      }
      return null;
    } finally {
      if (!controller.signal.aborted) setCatalogLoading(false);
    }
  };

  const read = async (discovery?: Promise<ModelCatalog | null>) => {
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
      const options = discovery ? await discovery : catalog;
      if (controller.signal.aborted || request.current !== controller) return;
      setSaved(current);
      // A refresh may overlap typing; only a pristine, unchanged draft can be replaced.
      if (!preserve && version === edits.current) {
        if (!saved || !sameValues(saved, current))
          setDraft(draftFrom(current, options));
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
    if (active) void read(readCatalog());
    return () => {
      request.current?.abort();
      request.current = null;
      catalogRequest.current?.abort();
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
      [effortField]: effortsFor(catalog, agent, next).includes(
        draft[effortField],
      )
        ? draft[effortField]
        : defaultEffort(catalog, agent, next),
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
      setDraft(draftFrom(current, catalog));
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
          <div className="settings-catalog-heading">
            <h2 id={`${id}-models`}>Code agent 模型</h2>
            <Button
              type="button"
              className="btn"
              size="icon"
              aria-label="刷新模型目录"
              title="刷新模型目录"
              disabled={catalogLoading || loading || pending}
              onClick={() => void readCatalog()}
            >
              <RefreshCw aria-hidden="true" />
            </Button>
          </div>
          <div aria-live="polite" className="settings-catalog-notice">
            {catalogLoading ? (
              "正在读取模型目录…"
            ) : catalogError ? (
              <span className="settings-error">{catalogError}</span>
            ) : (
              (["codex", "amp"] as const).map((agent) => {
                const group = catalog?.[agent];
                if (!group || (group.status === "ok" && group.models.length))
                  return null;
                const label = agent === "codex" ? "Codex" : "Amp";
                const message = {
                  ok: "目录未返回模型",
                  partial: "模型目录部分可用",
                  unavailable: "模型目录不可用",
                }[group.status];
                return (
                  <div key={agent}>
                    {label} {message}
                  </div>
                );
              })
            )}
          </div>
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
              const efforts = effortsFor(catalog, agent, draft[modelField]);
              const currentEffort =
                saved?.[modelField] === draft[modelField]
                  ? saved[effortField]
                  : undefined;
              const hasEfforts =
                efforts.length > 0 || !!draft[effortField] || !!currentEffort;
              return (
                <div className="settings-agent" key={agent}>
                  <SettingSelect
                    label={`${label} 模型`}
                    options={catalog?.[agent].models ?? []}
                    savedValue={saved?.[modelField]}
                    missingSuffix=" · 目录外"
                    id={`${id}-${agent}`}
                    title={draft[modelField] || undefined}
                    value={draft[modelField]}
                    disabled={!saved || pending}
                    onChange={(event) => changeModel(agent, event.target.value)}
                  >
                    <option value="">
                      {agent === "codex"
                        ? "不指定（原生默认）"
                        : "不指定（默认 low）"}
                    </option>
                  </SettingSelect>
                  <SettingSelect
                    options={efforts.map((level) => ({
                      id: level,
                      displayName: level === "medium" ? "mid" : level,
                    }))}
                    savedValue={currentEffort}
                    missingSuffix=" · 目录外"
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
                  </SettingSelect>
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
                    setDraft(draftFrom(saved, catalog));
                    setShowServer(false);
                    setWrite({ kind: "idle" });
                  }}
                >
                  <Check aria-hidden="true" />
                  使用服务器设置
                </Button>
              </section>
            )}
            <SettingActions
              submitLabel={pending ? "正在保存…" : "保存设置"}
              disabled={
                !saved ||
                !dirty ||
                pending ||
                loading ||
                locked ||
                missingEffort
              }
            >
              <Button
                type="button"
                className="btn"
                disabled={loading || pending}
                onClick={() => void read()}
              >
                <RefreshCw aria-hidden="true" />
                {write.kind === "unknown" ? "回读核对" : "重新读取设置"}
              </Button>
            </SettingActions>
          </form>
        </section>
        <SkillRepositorySetting refresh={refresh} />
        <FontSettings />
      </div>
    </main>
  );
}
