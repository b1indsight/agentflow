import type { TaskContent } from "./types";

/** UI-only title derivation; API and CLI callers continue to provide explicit title/body. */
export function taskContentFromText(
  body: string,
): { ok: true; task: TaskContent } | { ok: false; error: string } {
  if (body.includes("\0"))
    return { ok: false, error: "任务内容不能包含 NUL 字符。" };
  if (new TextEncoder().encode(body).length > 32768)
    return {
      ok: false,
      error: "任务内容不能超过 32768 字节（32 KiB UTF-8），请缩短内容。",
    };
  const first = body.split(/\r\n|[\r\n]/).find((line) => line.trim());
  if (!first) return { ok: false, error: "任务内容不能全部为空白。" };
  return {
    ok: true,
    task: { title: Array.from(first.trim()).slice(0, 256).join(""), body },
  };
}
