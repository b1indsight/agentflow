import assert from "node:assert/strict";
import { test } from "node:test";
import { taskContentFromText } from "./flowInput";

test("direct task titles count Unicode code points while preserving every body character", () => {
  // A pasted task can start with empty lines and exceed the title limit with emoji.
  const firstLine = "中文😀".repeat(100);
  const body = `\n \t\n  ${firstLine}  \n\n正文保留空格。  \n`;
  const result = taskContentFromText(body);
  assert.deepEqual(result, {
    ok: true,
    task: { title: Array.from(firstLine).slice(0, 256).join(""), body },
  });
});

test("direct task content rejects blank, NUL and UTF-8 overflow without truncating", () => {
  // Browser string length undercounts multibyte text: 8192 emoji are exactly 32 KiB.
  const body = "😀".repeat(8192);
  assert.equal(taskContentFromText(body).ok, true);
  for (const invalid of ["", "\n \t\r\n", "任务\0正文", `${body}a`])
    assert.equal(taskContentFromText(invalid).ok, false);
  assert.equal(taskContentFromText(`${"中".repeat(10922)}ab`).ok, true);
  assert.equal(taskContentFromText(`${"中".repeat(10922)}abc`).ok, false);
});
