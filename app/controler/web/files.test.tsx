import assert from "node:assert/strict";
import { test } from "node:test";
import { readFile, workerFileLink } from "./files";

const origin = "https://console.example.test";
const id = "0123456789abcdef0123456789abcdef";
const path = `/api/files/${id}`;

test("worker file destinations require an exact same-origin file route", () => {
  for (const url of [path, `${origin}${path}`])
    assert.deepEqual(workerFileLink(url, origin), { id, name: null });
  assert.deepEqual(
    workerFileLink(
      `${path}#name=${encodeURIComponent("报告 (100%#).md")}`,
      origin,
    ),
    { id, name: "报告 (100%#).md" },
  );
  for (const url of [
    `https://other.test${path}`,
    `${path}?download=1`,
    `${path}#line-1`,
    `${path}#name=..%2Fsecret`,
    `${path}#name=%5Csecret`,
    `${path}#name=%00x`,
    `${path}#name=%E2%80%AEx`,
    `${path}#name=%FF`,
    `${path}#name=${"a".repeat(3073)}`,
    `${path}/`,
    path.toUpperCase(),
    `../api/files/${id}`,
    `/other/..${path}`,
    `javascript:${path}`,
    `https://[invalid]${path}`,
    `${path}\n`,
    `https://console.example.test\n${path}`,
  ])
    assert.equal(workerFileLink(url, origin), null, url);
});

test("file reads preserve raw bytes and the authenticated cancellable download contract", async (t) => {
  const bytes = new Uint8Array([0, 255, 1, 128]);
  const signal = new AbortController().signal;
  t.mock.method(
    globalThis,
    "fetch",
    async (input: Parameters<typeof fetch>[0], options?: RequestInit) => {
      assert.equal(input, path);
      assert.equal(options?.credentials, "same-origin");
      assert.equal(options?.cache, "no-store");
      assert.equal(options?.signal, signal);
      assert.equal(options?.redirect, "error");
      return new Response(bytes);
    },
  );
  assert.deepEqual(await readFile(id, signal), bytes);
});

test("file reads enforce the actual 512 KiB stream budget and cancel oversized bodies", async (t) => {
  let cancelled = false;
  let chunks = 0;
  t.mock.method(
    globalThis,
    "fetch",
    async () =>
      new Response(
        new ReadableStream({
          pull(controller) {
            controller.enqueue(new Uint8Array(chunks++ ? 1 : 512 * 1024));
          },
          cancel() {
            cancelled = true;
          },
        }),
      ),
  );
  await assert.rejects(readFile(id, new AbortController().signal), {
    message: "file_too_large",
    status: 413,
  });
  assert.equal(cancelled, true);
});

test("file reads accept empty and boundary files and retain API error codes", async (t) => {
  let response = new Response(new Uint8Array());
  t.mock.method(globalThis, "fetch", async () => response);
  const signal = new AbortController().signal;
  assert.equal((await readFile(id, signal)).length, 0);
  response = new Response(new Uint8Array(512 * 1024));
  assert.equal((await readFile(id, signal)).length, 512 * 1024);
  response = new Response(JSON.stringify({ detail: "file_changed" }), {
    status: 409,
  });
  await assert.rejects(readFile(id, signal), {
    message: "file_changed",
    status: 409,
  });
});
