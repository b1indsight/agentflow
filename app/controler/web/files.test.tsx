import assert from "node:assert/strict";
import { test } from "node:test";
import { previewFile, readFile, workerFileLink } from "./files";

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

test("Amp display markers require both a same-origin file ID and a canonical attachment URL", () => {
  // The fragment is untrusted display metadata, not permission to proxy a URL or read amp-media-source.
  const name = "截图 (100%#).png";
  const ampUrl = `https://ampcode.com/user-content/attachments/${"a".repeat(64)}-${encodeURIComponent(name)}`;
  const marker = `${path}#name=${encodeURIComponent(name)}&amp=${encodeURIComponent(ampUrl)}`;
  assert.deepEqual(workerFileLink(marker, origin), { id, name, ampUrl });
  assert.deepEqual(workerFileLink(`${origin}${marker}`, origin), {
    id,
    name,
    ampUrl,
  });
  for (const invalid of [
    `https://other.test${marker}`,
    marker.replace(id, id.toUpperCase()),
    marker.replace("#name=", "#amp="),
    `${marker}&amp-media-source=%2Fetc%2Fpasswd`,
    `${marker}&unknown=value`,
    marker.replace("&amp=", "&unknown="),
    `${path}#name=preview.png&amp=%FF`,
    ...[
      ampUrl.replace("https:", "http:"),
      ampUrl.replace("ampcode.com", "ampcode.com.evil.test"),
      ampUrl.replace("ampcode.com", "user@ampcode.com"),
      ampUrl.replace("ampcode.com", "ampcode.com:443"),
      ampUrl.replace("a".repeat(64), "A".repeat(64)),
      ampUrl.replace("attachments/", "attachments/../"),
      ampUrl.replace("attachments/", "attachments%2F"),
      `${ampUrl}?download=1`,
      `${ampUrl}#amp-media-source=/etc/passwd`,
      ampUrl.replace(encodeURIComponent(name), "..%2Fsecret.png"),
      ampUrl.replace(encodeURIComponent(name), "bad%5Cname.png"),
      ampUrl.replace(encodeURIComponent(name), "bad%00name.png"),
    ].map(
      (url) =>
        `${path}#name=${encodeURIComponent(name)}&amp=${encodeURIComponent(url)}`,
    ),
  ])
    assert.equal(workerFileLink(invalid, origin), null, invalid);
});

test("shared file previews require raster signatures and a successful decoder, never executable source", async (t) => {
  // A .png hint can contain HTML/SVG or damaged bytes; the inline and file viewers must agree.
  let closed = false;
  let broken = false;
  const descriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "createImageBitmap",
  );
  Object.defineProperty(globalThis, "createImageBitmap", {
    configurable: true,
    value: async () => {
      if (broken) throw new Error("bad image");
      return {
        close() {
          closed = true;
        },
      };
    },
  });
  t.after(() => {
    if (descriptor)
      Object.defineProperty(globalThis, "createImageBitmap", descriptor);
    else Reflect.deleteProperty(globalThis, "createImageBitmap");
  });
  const png = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 0]);
  assert.equal((await previewFile(png)).kind, "image");
  assert.equal(closed, true);
  broken = true;
  assert.deepEqual(await previewFile(png), { kind: "binary" });
  for (const text of ["<svg onload='alert(1)'/>", "<script>alert(1)</script>"])
    assert.deepEqual(await previewFile(new TextEncoder().encode(text)), {
      kind: "text",
      text,
    });
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
