/** Exclude closed details descendants even when the browser retains their layout rectangles. */
export function focusableControls(container: HTMLElement | null | undefined) {
  return [
    ...(container?.querySelectorAll<HTMLElement>(
      "button:not(:disabled),summary,a[href],input:not(:disabled),[tabindex='0']:not(:disabled)",
    ) ?? []),
  ].filter((control) => control.checkVisibility({ checkVisibilityCSS: true }));
}
