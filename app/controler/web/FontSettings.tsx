import { useState } from "react";
import { Save } from "lucide-react";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/input";

const fields = [
  { name: "body-font", label: "正文字体", placeholder: "默认：系统字体" },
  {
    name: "code-font",
    label: "代码字体",
    placeholder: "默认：Monaspace Neon",
  },
] as const;

function validFamily(value: string) {
  return !value || CSS.supports("font-family", `${value}, sans-serif`);
}

/** Restore browser-local preferences before the first render on any console route. */
export function restoreFonts() {
  try {
    for (const { name } of fields) {
      const value = localStorage.getItem(`agentflow.${name}`);
      if (value && validFamily(value))
        document.documentElement.style.setProperty(`--${name}`, value);
    }
  } catch {
    // Storage may be disabled. The stylesheet defaults still work.
  }
}

export function FontSettings() {
  const [message, setMessage] = useState("");
  const style = document.documentElement.style;
  return (
    <form
      aria-label="显示字体"
      onSubmit={(event) => {
        event.preventDefault();
        const data = new FormData(event.currentTarget);
        let persisted = true;
        for (const { name } of fields) {
          const value = String(data.get(name)).trim();
          style.setProperty(`--${name}`, value);
          try {
            if (value) localStorage.setItem(`agentflow.${name}`, value);
            else localStorage.removeItem(`agentflow.${name}`);
          } catch {
            persisted = false;
          }
        }
        setMessage(
          persisted
            ? "字体已保存。"
            : "字体已应用到当前页面，但浏览器未能保存设置。",
        );
      }}
    >
      <div className="settings-catalog-heading">
        <h2>显示字体</h2>
      </div>
      <p className="settings-catalog-notice">
        输入本机字体名称，多个字体用英文逗号分隔。留空恢复默认，仅保存在当前浏览器。
      </p>
      <p className="settings-catalog-notice">
        默认代码字体为 GitHub 的{" "}
        <a
          href="https://github.com/githubnext/monaspace/tree/v1.400"
          target="_blank"
          rel="noreferrer"
        >
          Monaspace Neon
        </a>
        ，已内置，无需安装。次选 Microsoft Sans Serif。
      </p>
      <div className="settings-agent">
        {fields.map(({ name, label, placeholder }) => (
          <div key={name} className="settings-field">
            <label htmlFor={name}>{label}</label>
            <Input
              id={name}
              name={name}
              placeholder={placeholder}
              defaultValue={style.getPropertyValue(`--${name}`)}
              spellCheck={false}
              onChange={({ currentTarget }) => {
                currentTarget.setCustomValidity(
                  validFamily(currentTarget.value.trim())
                    ? ""
                    : "请输入有效的字体名称或字体列表。",
                );
                setMessage("");
              }}
            />
          </div>
        ))}
      </div>
      <p>
        预览：正文 AgentFlow · <code>const flow = 42;</code>
      </p>
      <div className="settings-actions">
        <span aria-live="polite">{message}</span>
        <Button type="submit" className="btn settings-save">
          <Save aria-hidden="true" />
          保存字体
        </Button>
      </div>
    </form>
  );
}
