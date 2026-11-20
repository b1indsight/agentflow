/** Validate without trimming, normalizing line endings or deriving a display title. */
export function taskContentFromText(
  body: string,
): { ok: true; task: string } | { ok: false; error: string } {
  if (body.includes("\0"))
    return { ok: false, error: "任务内容不能包含 NUL 字符。" };
  if (new TextEncoder().encode(body).length > 32768)
    return {
      ok: false,
      error: "任务内容不能超过 32768 字节（32 KiB UTF-8），请缩短内容。",
    };
  if (!body.trim()) return { ok: false, error: "任务内容不能全部为空白。" };
  return { ok: true, task: body };
}

/** Replace a textarea selection while keeping raw clipboard line endings outside the DOM. */
export function replaceTaskText(
  text: string,
  start: number,
  end: number,
  inserted: string,
): string {
  // Textarea offsets count CRLF as one LF; string offsets count both characters.
  const offsets = [0];
  for (let raw = 0; raw < text.length; raw++) {
    if (text[raw] === "\r" && text[raw + 1] === "\n") raw++;
    offsets.push(raw + 1);
  }
  return text.slice(0, offsets[start]) + inserted + text.slice(offsets[end]);
}

/** Apply a native edit; the resulting caret disambiguates repeated text and mixed newlines. */
export function updateTaskText(
  text: string,
  value: string,
  caret: number,
): string {
  const prior = text.replace(/\r\n?/g, "\n");
  let end = prior.length;
  let nextEnd = value.length;
  while (end > 0 && nextEnd > caret && prior[end - 1] === value[nextEnd - 1]) {
    end--;
    nextEnd--;
  }
  let start = 0;
  while (start < end && start < nextEnd && prior[start] === value[start])
    start++;
  return replaceTaskText(text, start, end, value.slice(start, nextEnd));
}
