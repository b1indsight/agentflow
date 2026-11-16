import assert from "node:assert/strict";
import { after, test } from "node:test";
import { getActivity, getContent, getDiff, getThread } from "./api";
import type { ActivityPage, ContentPage } from "./types";

Object.defineProperty(globalThis, "location", {
  configurable: true,
  value: new URL("https://console.test"),
});
after(() => Reflect.deleteProperty(globalThis, "location"));

const content: ContentPage = {
  key: "entry",
  version: 2,
  text: "正文",
  total: 2,
  next_offset: null,
  previous_version: 1,
};
const replies: ActivityPage = {
  items: [],
  snapshot: 2,
  total: 0,
  next_cursor: null,
  has_more: false,
  sources: [],
  changes: {},
};

test("session reads reuse complete identities, while refresh always reaches the server", async (t) => {
  // The same entry can occur in another Flow, version, segment or pinned reply snapshot.
  const fetch = t.mock.method(globalThis, "fetch", async (url: URL) =>
    Response.json(url.pathname.endsWith("/content") ? content : replies),
  );
  const reads = [
    () => getContent(1, "entry", 2, 0, 20),
    () => getContent(2, "entry", 2, 0, 20),
    () => getContent(1, "other", 2, 0, 20),
    () => getContent(1, "entry", 3, 0, 20),
    () => getContent(1, "entry", 2, 1, 20),
    () => getContent(1, "entry", 2, 0, 10),
    () => getDiff(1, "entry", 2),
    () => getDiff(1, "entry", 3),
    () => getThread(1, "entry", null, 2),
    () => getThread(2, "entry", null, 2),
    () => getThread(1, "other", null, 2),
    () => getThread(1, "entry", "next", 2),
    () => getThread(1, "entry", null, 3),
  ];
  for (const [i, read] of reads.entries()) {
    const first = await read();
    assert.deepEqual(await read(), first);
    assert.equal(fetch.mock.callCount(), i + 1);
  }
  await getActivity(1, { refresh: true, since: 2 });
  await getActivity(1, { refresh: true, since: 2 });
  assert.equal(fetch.mock.callCount(), reads.length + 2);
});

test("failed and aborted reads do not poison the session cache", async (t) => {
  // Navigation can abort even after fetch resolves; a later mount must still be able to retry.
  const controller = new AbortController();
  let outcome = "failure";
  const fetch = t.mock.method(globalThis, "fetch", async () => {
    if (outcome === "failure") return new Response(null, { status: 502 });
    if (outcome === "abort") controller.abort();
    return Response.json(content);
  });
  const read = (signal?: AbortSignal) =>
    getContent(10, "entry", 2, 0, 20, signal);
  await assert.rejects(read(), { status: 502 });
  outcome = "abort";
  await assert.rejects(read(controller.signal), { name: "AbortError" });
  outcome = "success";
  assert.deepEqual(await read(), content);
  assert.deepEqual(await read(), content);
  await assert.rejects(read(controller.signal), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 3);
});

test("an aborted overlapping read cannot replace another caller's cached response", async (t) => {
  // A row can remount before its old fetch settles; its cancelled result must not win the cache race.
  const pending: Array<(response: Response) => void> = [];
  const fetch = t.mock.method(
    globalThis,
    "fetch",
    () => new Promise<Response>((resolve) => pending.push(resolve)),
  );
  const controller = new AbortController();
  const read = (signal?: AbortSignal) =>
    getContent(30, "entry", 2, 0, 20, signal);
  const oldRead = read(controller.signal);
  const newRead = read();
  const aborted = assert.rejects(oldRead, { name: "AbortError" });
  controller.abort();
  pending[1](Response.json(content));
  assert.deepEqual(await newRead, content);
  pending[0](Response.json({ ...content, text: "已取消的响应" }));
  await aborted;
  assert.deepEqual(await read(), content);
  assert.equal(fetch.mock.callCount(), 2);
});

test("session response caches evict least-recently-used data and can reread it", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async (url: URL) =>
    Response.json(url.pathname.endsWith("/content") ? content : replies),
  );
  // Scrolling many segments/threads must not retain every response for the page's lifetime.
  for (const [capacity, read] of [
    [64, (i: number) => getContent(20, "entry", 2, i, 20)],
    [12, (i: number) => getThread(20, `thread-${i}`, null, 2)],
  ] as const) {
    for (let i = 0; i < capacity; i++) await read(i);
    const before = fetch.mock.callCount();
    await read(0);
    await read(capacity);
    await read(0);
    assert.equal(fetch.mock.callCount(), before + 1);
    await read(1);
    assert.equal(fetch.mock.callCount(), before + 2);
  }
});

test("response byte budgets include Unicode payloads and metadata without rejecting oversized reads", async (t) => {
  const largeContent = { ...content, text: "中".repeat(1024 * 1024) };
  const oversized = { ...content, text: "中".repeat(2 * 1024 * 1024) };
  const largeReplies: ActivityPage = {
    ...replies,
    sources: [
      {
        source: "worker",
        status: "partial",
        reason: "😀".repeat(256 * 1024),
        last_success_at: null,
        limitations: [],
      },
    ],
  };
  const fetch = t.mock.method(globalThis, "fetch", async (url: URL) =>
    Response.json(
      url.searchParams.get("key") === "oversized"
        ? oversized
        : url.pathname.endsWith("/content")
          ? largeContent
          : largeReplies,
    ),
  );
  // Two entries fit the count caps, but exceed each cache's byte budget once metadata is counted.
  for (const read of [
    (i: number) => getContent(40, `entry-${i}`, 2, 0, 20),
    (i: number) => getThread(40, `thread-${i}`, null, 2),
  ]) {
    const before = fetch.mock.callCount();
    await read(0);
    await read(1);
    await read(1);
    assert.equal(fetch.mock.callCount(), before + 2);
    await read(0);
    assert.equal(fetch.mock.callCount(), before + 3);
  }
  const before = fetch.mock.callCount();
  for (let i = 0; i < 2; i++)
    assert.deepEqual(await getDiff(40, "oversized", 2), oversized);
  await getContent(40, "entry-0", 2, 0, 20);
  assert.equal(fetch.mock.callCount(), before + 2);
});
