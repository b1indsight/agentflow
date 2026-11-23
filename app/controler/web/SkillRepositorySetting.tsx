import { useEffect, useId, useRef, useState } from "react";
import { ArrowDown, RefreshCw, Save } from "lucide-react";
import { api, getFlowTargets, settingPath } from "./api";
import { Button } from "./components/ui/button";
import { SettingSelect } from "./SettingSelect";
import type { FlowTarget } from "./types";

const path = settingPath("skill-repository");

/** Save this choice independently of the coding-model draft. */
export function SkillRepositorySetting({ refresh }: { refresh: number }) {
  const id = useId();
  const [saved, setSaved] = useState<string | null>();
  const [choice, setChoice] = useState("");
  const [targets, setTargets] = useState<FlowTarget[]>([]);
  const [pageRequest, setPageRequest] = useState<{
    refresh: number;
    cursor: string;
  }>();
  const cursor = pageRequest?.refresh === refresh ? pageRequest.cursor : null;
  const lastRefresh = useRef(refresh);
  const [next, setNext] = useState<string | null>(null);
  const [readError, setReadError] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setSaved(undefined);
    setMessage("");
    api<string | null>(path, {}, controller.signal)
      .then((repo) => {
        if (controller.signal.aborted) return;
        setSaved(repo);
        setChoice(repo ?? "");
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setMessage("无法读取已保存的仓库，请刷新设置。");
      });
    return () => controller.abort();
  }, [refresh, reload]);

  useEffect(() => {
    const controller = new AbortController();
    setNext(null);
    setReadError("");
    const force = refresh !== lastRefresh.current;
    lastRefresh.current = refresh;
    getFlowTargets(cursor, controller.signal, force)
      .then((page) => {
        if (controller.signal.aborted) return;
        setTargets((prior) =>
          cursor ? [...prior, ...page.items] : page.items,
        );
        setNext(page.next_cursor);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setReadError("项目列表读取失败，请刷新设置重试。");
      });
    return () => controller.abort();
  }, [cursor, refresh]);

  const save = async () => {
    if (saved === undefined || saving) return;
    const target = targets.find((item) => item.repo === choice);
    if (choice && !target) return;
    setSaving(true);
    setMessage("");
    try {
      const repo = await api<string | null>(path, {}, undefined, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target: target ?? null,
          expected_repository: saved,
        }),
      });
      setSaved(repo);
      setChoice(repo ?? "");
      setMessage("Skill 仓库设置已保存。");
    } catch (error) {
      setSaved(undefined);
      setMessage(
        `未能确认保存结果，请重新读取：${error instanceof Error ? error.message : "请求失败"}`,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <SettingSelect
        label="Skill 仓库"
        options={targets.map(({ repo }) => ({ id: repo, displayName: repo }))}
        id={id}
        value={choice}
        disabled={saved === undefined || saving}
        onChange={(event) => {
          setChoice(event.target.value);
          setMessage("");
        }}
      >
        <option value="">不指定</option>
      </SettingSelect>
      <div className="settings-feedback" aria-live="polite">
        {readError && <p className="settings-error">{readError}</p>}
        {message && <p>{message}</p>}
      </div>
      <div className="settings-actions">
        {next && (
          <Button
            type="button"
            className="btn"
            onClick={() => setPageRequest({ refresh, cursor: next })}
          >
            <ArrowDown />
            更多项目
          </Button>
        )}
        {saved === undefined && message && (
          <Button
            type="button"
            className="btn"
            onClick={() => {
              setMessage("");
              setReload(reload + 1);
            }}
          >
            <RefreshCw />
            重新读取仓库
          </Button>
        )}
        <Button
          type="submit"
          className="btn settings-save"
          disabled={saved === undefined || saving || choice === (saved ?? "")}
        >
          <Save />
          保存仓库
        </Button>
      </div>
    </form>
  );
}
