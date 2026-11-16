import { type ComponentProps, useLayoutEffect, useRef } from "react";
import { replaceTaskText, updateTaskText } from "./flowInput";

/** Controlled task text shared by creation and revision, preserving pasted line endings. */
export function FlowTaskInput({
  value,
  onValueChange,
  onSubmitShortcut,
  ...props
}: Omit<
  ComponentProps<"textarea">,
  "value" | "onChange" | "onPaste" | "onKeyDown"
> & {
  value: string;
  onValueChange: (value: string) => void;
  onSubmitShortcut?: () => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const pastedCursor = useRef<number | null>(null);
  useLayoutEffect(() => {
    if (pastedCursor.current !== null) {
      textarea.current?.setSelectionRange(
        pastedCursor.current,
        pastedCursor.current,
      );
      pastedCursor.current = null;
    }
  }, [value]);

  return (
    <textarea
      {...props}
      ref={textarea}
      value={value.replace(/\r\n?/g, "\n")}
      onChange={(event) =>
        onValueChange(
          updateTaskText(
            value,
            event.target.value,
            event.target.selectionStart,
          ),
        )
      }
      onPaste={(event) => {
        // Native paste normalizes CRLF before onChange can observe it.
        event.preventDefault();
        const input = event.currentTarget;
        const pasted = event.clipboardData.getData("text/plain");
        const next = replaceTaskText(
          value,
          input.selectionStart,
          input.selectionEnd,
          pasted,
        );
        const cursor =
          input.selectionStart + pasted.replace(/\r\n?/g, "\n").length;
        if (next === value) input.setSelectionRange(cursor, cursor);
        else pastedCursor.current = cursor;
        onValueChange(next);
      }}
      onKeyDown={(event) => {
        if (
          onSubmitShortcut &&
          event.key === "Enter" &&
          (event.ctrlKey || event.metaKey) &&
          !event.nativeEvent.isComposing &&
          event.nativeEvent.keyCode !== 229
        ) {
          event.preventDefault();
          onSubmitShortcut();
        }
      }}
    />
  );
}
