import type { ComponentProps, PropsWithChildren } from "react";
import { Save } from "lucide-react";
import { Button } from "./components/ui/button";

type FieldProps = {
  id: string;
  label?: string;
};

export function SettingField({
  id,
  label,
  children,
}: PropsWithChildren<FieldProps>) {
  return (
    <div className="settings-field">
      {label && <label htmlFor={id}>{label}</label>}
      {children}
    </div>
  );
}

type SelectProps = ComponentProps<"select"> &
  FieldProps & {
    value: string;
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
}: SelectProps) {
  const missing = [...new Set([value, savedValue])].filter(
    (id): id is string => !!id && !options.some((option) => option.id === id),
  );
  return (
    <SettingField id={select.id} label={label}>
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
    </SettingField>
  );
}

export function SettingActions({
  submitLabel,
  disabled,
  children,
}: PropsWithChildren<{
  submitLabel: string;
  disabled?: boolean;
}>) {
  return (
    <div className="settings-actions">
      {children}
      <Button type="submit" className="btn settings-save" disabled={disabled}>
        <Save aria-hidden="true" />
        {submitLabel}
      </Button>
    </div>
  );
}
