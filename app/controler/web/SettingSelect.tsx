import type { ComponentProps } from "react";

type Props = ComponentProps<"select"> & {
  value: string;
  label?: string;
  options: readonly { id: string; displayName: string }[];
  savedValue?: string | null;
  missingSuffix?: string;
};

/** Keep current values selectable while an option list is incomplete or has changed. */
export function SettingSelect({
  label,
  options,
  value,
  savedValue,
  missingSuffix = "",
  children,
  ...select
}: Props) {
  const missing = [...new Set([value, savedValue])].filter(
    (id): id is string => !!id && !options.some((option) => option.id === id),
  );
  return (
    <div className="settings-field">
      {label && <label htmlFor={select.id}>{label}</label>}
      <select {...select} value={value}>
        {children}
        {missing.map((id) => (
          <option key={id} value={id}>
            {id}
            {missingSuffix}
          </option>
        ))}
        {options.map((option) => (
          <option key={option.id} value={option.id}>
            {option.displayName}
          </option>
        ))}
      </select>
    </div>
  );
}
