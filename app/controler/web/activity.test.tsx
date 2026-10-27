import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ActivitySummary,
  ActivityView,
  buildConversationRows,
  conversationProvenanceRange,
  sameActivityGroup,
} from "./activity";
import type { ActivityItem, ActivityPage } from "./types";
import {
  ACTIVITY_CACHE_ROWS,
  ACTIVITY_PROVENANCE_ROWS,
  activityIdentity,
  readConversationProvenance,
  refreshConversationSnapshot,
  type ConversationState,
} from "./useConversation";

const ownership = {
  attempt_id: 1,
  session_id: "session-a",
  session_generation: 0,
};

function record(
  key: string,
  overrides: Partial<ActivityItem> = {},
): ActivityItem {
  const body = overrides.body ?? `body ${key}`;
  return {
    key,
    version: 1,
    source: "worker",
    kind: "tool_call",
    author: "assistant",
    occurred_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:00:00Z",
    observed_at: "2026-10-27T00:00:00Z",
    title: "exec",
    body,
    body_length: Array.from(body).length,
    url: null,
    parent_key: null,
    deleted: false,
    prior: false,
    previous_version: null,
    metadata: ownership,
    ...overrides,
  };
}

function stateFor(items: ActivityItem[], snapshot = 1): ConversationState {
  return {
    records: items.map(activityIdentity),
    items: new Map(items.map((item, index) => [index, item])),
    pages: Array.from({ length: Math.ceil(items.length / 20) }, (_, page) => ({
      cursor: page ? String(page * 20) : null,
      indexes: items
        .slice(page * 20, page * 20 + 20)
        .map((_, i) => page * 20 + i),
    })),
    snapshot,
    total: items.length,
    next_cursor: null,
    has_more: false,
    sources: [],
    changes: {},
    busy: false,
    error: null,
  };
}

function page(
  items: ActivityItem[],
  next_cursor: string | null = null,
  snapshot = 2,
): ActivityPage {
  return {
    items,
    snapshot,
    total: items.length,
    next_cursor,
    has_more: next_cursor !== null,
    sources: [],
    changes: {},
  };
}

function mockActivity(
  t: TestContext,
  respond: (url: URL) => ActivityPage | Promise<ActivityPage>,
) {
  const location = Object.getOwnPropertyDescriptor(globalThis, "location");
  Object.defineProperty(globalThis, "location", {
    configurable: true,
    value: { origin: "https://console.example.test" },
  });
  t.after(() => {
    if (location) Object.defineProperty(globalThis, "location", location);
    else Reflect.deleteProperty(globalThis, "location");
  });
  return t.mock.method(
    globalThis,
    "fetch",
    async (input: Parameters<typeof fetch>[0]) => {
      const result = await respond(new URL(String(input)));
      return new Response(JSON.stringify(result), { status: 200 });
    },
  );
}

test("identity projection preserves ownership values and missingness without retaining payloads", () => {
  // Body eviction must leave the same grouping inputs, without retaining large source metadata.
  const full = record("identity", {
    metadata: {
      attempt_id: 0,
      session_id: null,
      call_id: "",
      enormous: "x".repeat(4000),
    },
  });
  const identity = activityIdentity(full);
  assert.deepEqual(identity.metadata, {
    attempt_id: 0,
    session_id: null,
    call_id: "",
  });
  assert.equal(Object.hasOwn(identity.metadata, "session_generation"), false);
  assert.equal(Object.hasOwn(identity, "body"), false);
  assert.equal(Object.hasOwn(identity, "title"), false);
  assert.equal(Object.hasOwn(identity, "updated_at"), false);
  assert.notEqual(identity.metadata, full.metadata);
});

test("folded long groups have one identity-only summary and stable independent body rows", () => {
  // A tool run may exceed both the mounted-row and full-body cache budgets.
  const records = Array.from({ length: 300 }, (_, index) =>
    activityIdentity(
      record(`tool-${index}`, {
        kind: index % 2 ? "tool_result" : "tool_call",
        author: index % 2 ? "tool" : "assistant",
        metadata: { ...ownership, call_id: `call-${Math.floor(index / 2)}` },
      }),
    ),
  );
  const folded = buildConversationRows(records, new Set());
  assert.deepEqual(folded, [
    {
      kind: "summary",
      key: "summary:tool-0",
      expanded: false,
      group: {
        key: "tool-0",
        kind: "tool",
        first: 0,
        last: 299,
        calls: 150,
        results: 150,
      },
    },
  ]);
  const expanded = buildConversationRows(records, new Set(["tool-0"]));
  assert.equal(expanded.length, 301);
  assert.deepEqual(expanded[0], { ...folded[0], expanded: true });
  assert.equal(new Set(expanded.map((row) => row.key)).size, expanded.length);
  assert.deepEqual(expanded[2], {
    kind: "body",
    key: "body:tool-1",
    rawIndex: 1,
    groupKey: "tool-0",
    pairedCallKey: "tool-0",
  });
  // A reader can be in the middle of a run when refresh backfills or removes its first member.
  const earlier = activityIdentity(record("earlier-tool"));
  for (const refreshed of [
    records,
    [earlier, ...records],
    [earlier, ...records.slice(1)],
  ]) {
    const reading = buildConversationRows(refreshed, new Set(["tool-101"]));
    assert.equal(reading.length, refreshed.length + 1);
    assert.ok(reading.some((row) => row.key === "body:tool-101"));
  }
  assert.deepEqual(buildConversationRows(records, new Set()), folded);
  assert.deepEqual(conversationProvenanceRange(records, 0), {
    first: 0,
    last: 299,
  });
});

test("tool projection pairs only preceding nonempty IDs and emits every result once beside its nearest call", () => {
  // Interleaving, repeated IDs and fan-out used to move ownership with the mounted window.
  const tool = (key: string, kind: string, call_id: unknown) =>
    activityIdentity(
      record(key, {
        kind,
        author: kind === "tool_call" ? "assistant" : "tool",
        metadata: { ...ownership, call_id },
      }),
    );
  const records = [
    tool("early", "tool_result", "a"),
    tool("a1", "tool_call", "a"),
    tool("b", "tool_call", "b"),
    tool("b-result", "tool_result", "b"),
    tool("a1-result", "tool_result", "a"),
    tool("a1-more", "tool_result", "a"),
    tool("a2", "tool_call", "a"),
    tool("a2-result", "tool_result", "a"),
    tool("empty-call", "tool_call", ""),
    tool("empty-result", "tool_result", ""),
    tool("missing-result", "tool_result", undefined),
    tool("unknown-result", "tool_result", "unknown"),
  ];
  const rows = buildConversationRows(records, new Set(["early"]));
  assert.deepEqual(
    rows
      .filter((row) => row.kind === "body")
      .map((row) => [records[row.rawIndex].key, row.pairedCallKey]),
    [
      ["early", null],
      ["a1", null],
      ["a1-result", "a1"],
      ["a1-more", "a1"],
      ["b", null],
      ["b-result", "b"],
      ["a2", null],
      ["a2-result", "a2"],
      ["empty-call", null],
      ["empty-result", null],
      ["missing-result", null],
      ["unknown-result", null],
    ],
  );
  assert.equal(rows.length, records.length + 1);
});

test("identity classifiers retain cross-source operations and worker ownership and history boundaries", () => {
  const call = record("call", { metadata: { ...ownership, call_id: "a" } });
  const result = record("result", {
    kind: "tool_result",
    author: "tool",
    metadata: { ...ownership, call_id: "a" },
  });
  for (const patch of [
    { metadata: { ...ownership, attempt_id: 2, call_id: "a" } },
    { metadata: { ...ownership, session_id: "other", call_id: "a" } },
    { metadata: { ...ownership, session_generation: 1, call_id: "a" } },
    { metadata: { call_id: "a" } },
    { metadata: { ...ownership, session_id: null, call_id: "a" } },
    { prior: true },
    { deleted: true },
    { parent_key: "thread" },
    { source: "other" },
    { kind: "thread" },
  ] satisfies Partial<ActivityItem>[]) {
    const other = { ...result, ...patch };
    assert.equal(
      sameActivityGroup(activityIdentity(call), activityIdentity(other)),
      false,
    );
    assert.equal(
      sameActivityGroup(activityIdentity(other), activityIdentity(call)),
      false,
    );
    const rows = buildConversationRows(
      [call, other].map(activityIdentity),
      new Set([call.key, other.key]),
    );
    assert.ok(
      rows.every((row) => row.kind !== "body" || row.pairedCallKey === null),
    );
  }
  assert.equal(sameActivityGroup(call, result), true);
  assert.equal(
    sameActivityGroup(activityIdentity(call), activityIdentity(result)),
    true,
  );
  assert.equal(
    sameActivityGroup(
      record("missing", { metadata: {} }),
      record("missing-too", { metadata: {} }),
    ),
    false,
  );
  const separated = [call, record("message", { kind: "message" }), result].map(
    activityIdentity,
  );
  const turns = buildConversationRows(
    separated,
    new Set([call.key, result.key]),
  );
  assert.equal(turns.filter((row) => row.kind === "summary").length, 2);
  assert.ok(
    turns.every((row) => row.kind !== "body" || row.pairedCallKey === null),
  );
  assert.deepEqual(conversationProvenanceRange(separated, 0), {
    first: 0,
    last: 2,
  });

  const check = record("check", {
    source: "commit.check_runs:one",
    kind: "check",
    author: "bot",
    metadata: {},
  });
  const event = record("event", {
    source: "pr.timeline",
    kind: "event",
    author: "bot",
    body_length: 0,
    metadata: {},
  });
  const last = record("last", {
    source: "commit.check_runs:two",
    kind: "check",
    author: "bot",
    metadata: {},
  });
  const rows = buildConversationRows(
    [check, event, last].map(activityIdentity),
    new Set(),
  );
  assert.deepEqual(rows, [
    {
      kind: "summary",
      key: "summary:check",
      expanded: false,
      group: {
        key: "check",
        kind: "operation",
        first: 0,
        last: 2,
        calls: 0,
        results: 0,
      },
    },
  ]);
  assert.deepEqual(
    conversationProvenanceRange([check, event, last].map(activityIdentity), 0),
    { first: 0, last: 2 },
  );
  assert.equal(
    conversationProvenanceRange([check, event, last].map(activityIdentity), 1),
    null,
  );
  assert.equal(
    buildConversationRows([activityIdentity(check)], new Set())[0].kind,
    "body",
  );
  assert.equal(sameActivityGroup(check, { ...event, body_length: 1 }), false);
  assert.equal(sameActivityGroup(check, { ...event, author: "other" }), false);
  assert.equal(sameActivityGroup(check, { ...event, source: "worker" }), false);
});

test("summary rendering needs no body while expanded result rows own only their own body and metadata", () => {
  const call = record("call", { body: "run command" });
  const result = record("result", {
    kind: "tool_result",
    author: "tool",
    body: "unique output",
    body_length: 13,
  });
  const rows = buildConversationRows(
    [call, result].map(activityIdentity),
    new Set(),
  );
  assert.equal(rows[0].kind, "summary");
  if (rows[0].kind !== "summary") return;
  const summary = renderToStaticMarkup(
    <ActivitySummary
      entry={activityIdentity(call)}
      group={rows[0].group}
      expanded={false}
      onToggle={() => assert.fail("render expanded a group")}
    />,
  );
  assert.match(summary, /工具活动/);
  assert.match(summary, /1 次调用 · 1 条结果/);
  assert.match(summary, /aria-expanded="false"/);
  assert.doesNotMatch(
    summary,
    /<pre|body call|run command|unique output|<article[^>]* hidden=/,
  );
  const resultBody = renderToStaticMarkup(
    <ActivityView entry={result} groupKey="call" pairedCallKey="call" />,
  );
  assert.equal((resultBody.match(/aria-label="工具摘要"/g) ?? []).length, 1);
  assert.match(resultBody, /<blockquote[^>]*data-paired-call="call"/);
  assert.match(resultBody, /unique output/);
  assert.doesNotMatch(
    resultBody,
    /run command|<details|hidden=""[^>]*data-entry=/,
  );
  const callBody = renderToStaticMarkup(
    <ActivityView entry={call} groupKey="call" />,
  );
  assert.match(callBody, /tool-call-name/);
  assert.doesNotMatch(callBody, /unique output/);
});

test("merged provenance labels use identity counts even when member bodies are absent", () => {
  const entry = record("first", { kind: "message" });
  const html = renderToStaticMarkup(
    <ActivityView
      entry={entry}
      provenance={{
        first: 0,
        last: 299,
        read: async () =>
          assert.fail("closed provenance must not read archive pages"),
      }}
    />,
  );
  assert.match(html, /来源信息与查询定位 · 300 条记录/);
  assert.doesNotMatch(html, /enormous|下一页来源记录/);
});

test("lazy provenance rereads only a bounded metadata page in its pinned snapshot", async (t) => {
  // A source group can outlive every cached body; opening Info must not fetch its whole history.
  const full = Array.from({ length: 45 }, (_, i) =>
    record(`record-${i}`, {
      kind: "message",
      metadata: { ...ownership, complete_source: `source-${i}` },
    }),
  );
  const state = stateFor(full, 2);
  state.items = new Map([[44, full[44]]]);
  const requested: (string | null)[] = [];
  mockActivity(t, (url) => {
    assert.equal(url.searchParams.get("snapshot"), "2");
    const cursor = url.searchParams.get("cursor");
    requested.push(cursor);
    const start = Number(cursor ?? 0);
    return page(
      full.slice(start, start + 20),
      start + 20 < full.length ? String(start + 20) : null,
    );
  });
  const result = await readConversationProvenance(
    42,
    state,
    { first: 18, last: 44 },
    0,
    new AbortController().signal,
  );
  assert.deepEqual(requested, [null, "20"]);
  assert.equal(result.items.length, ACTIVITY_PROVENANCE_ROWS);
  assert.equal(result.total, 27);
  assert.equal(result.next_offset, 20);
  assert.equal(result.items[0].key, "record-18");
  assert.equal(result.items[19].metadata.complete_source, "source-37");
  assert.equal(Object.hasOwn(result.items[0], "body"), false);
  assert.equal(state.items.size, 1);
  requested.length = 0;
  const tail = await readConversationProvenance(
    42,
    state,
    { first: 18, last: 44 },
    20,
    new AbortController().signal,
  );
  assert.deepEqual(requested, ["20", "40"]);
  assert.equal(tail.items.length, 7);
  assert.equal(tail.next_offset, null);
  assert.equal(tail.items[6].key, "record-44");
});

test("refresh follows the latest logical target while a page is waiting, without any DOM anchor", async (t) => {
  const full = ["a", "b", "c", "d"].map((key) => record(key));
  const before = stateFor(full);
  before.items.clear();
  let anchor: string | null = "d";
  let release!: (value: ActivityPage) => void;
  let waiting!: () => void;
  const blocked = new Promise<ActivityPage>((resolve) => {
    release = resolve;
  });
  const started = new Promise<void>((resolve) => {
    waiting = resolve;
  });
  const requested: (string | null)[] = [];
  mockActivity(t, (url) => {
    const cursor = url.searchParams.get("cursor");
    requested.push(cursor);
    if (!cursor) return page([full[0]], "next");
    assert.equal(url.searchParams.get("snapshot"), "2");
    assert.equal(cursor, "next");
    waiting();
    return blocked;
  });
  const refresh = refreshConversationSnapshot(
    42,
    before,
    () => anchor,
    new AbortController().signal,
  );
  await started;
  anchor = "a";
  release(page([full[1]], "unrelated"));
  const result = await refresh;
  assert.deepEqual(requested, [null, "next"]);
  assert.equal(result.anchorKey, "a");
  assert.deepEqual(
    result.state.records.map((item) => item.key),
    ["a", "b"],
  );
  assert.equal(before.items.size, 0);
});

test("refresh finds a newly selected later target and keeps identities after the body budget is exhausted", async (t) => {
  const full = Array.from({ length: 300 }, (_, i) =>
    record(`record-${String(i).padStart(3, "0")}`, {
      occurred_at: new Date(
        Date.parse("2026-10-27T00:00:00Z") + i * 1000,
      ).toISOString(),
    }),
  );
  const before = stateFor(full);
  before.items.clear();
  let anchor = full[0].key;
  const requested: (string | null)[] = [];
  mockActivity(t, (url) => {
    const cursor = url.searchParams.get("cursor");
    requested.push(cursor);
    if (!cursor) {
      anchor = full[259].key;
      assert.equal(url.searchParams.get("refresh"), "true");
      assert.equal(url.searchParams.get("since"), "1");
    } else assert.equal(url.searchParams.get("snapshot"), "2");
    const start = Number(cursor ?? 0);
    return {
      ...page(full.slice(start, start + 20), String(start + 20)),
      total: full.length,
    };
  });
  const result = await refreshConversationSnapshot(
    42,
    before,
    () => anchor,
    new AbortController().signal,
  );
  assert.equal(requested.length, 13);
  assert.equal(result.anchorKey, full[259].key);
  assert.equal(result.state.records.length, 260);
  assert.equal(result.state.items.size, ACTIVITY_CACHE_ROWS);
  assert.equal(result.state.items.has(0), false);
  const rows = buildConversationRows(result.state.records, new Set());
  assert.equal(rows[0].kind, "summary");
  if (rows[0].kind === "summary") assert.equal(rows[0].group.last, 259);
});

test("missing refresh targets fall back to known neighbors rather than fetching unrelated history", async (t) => {
  const previous = ["a", "b", "c"].map((key) => record(key));
  const before = stateFor(previous);
  const requested: (string | null)[] = [];
  let predecessorExists = true;
  mockActivity(t, (url) => {
    const cursor = url.searchParams.get("cursor");
    requested.push(cursor);
    if (!predecessorExists) return page([previous[2]], "unrelated");
    return cursor
      ? page([previous[2]], "unrelated")
      : page([previous[0]], "neighbor");
  });
  const result = await refreshConversationSnapshot(
    42,
    before,
    () => "b",
    new AbortController().signal,
  );
  assert.equal(result.anchorKey, "a");
  assert.deepEqual(requested, [null, "neighbor"]);
  assert.equal(result.state.has_more, true);
  requested.length = 0;
  predecessorExists = false;
  const successor = await refreshConversationSnapshot(
    42,
    before,
    () => "b",
    new AbortController().signal,
  );
  assert.equal(successor.anchorKey, "c");
  assert.deepEqual(requested, [null]);
});

test("refresh reuses unchanged snapshots and rejects mixed snapshot pages", async (t) => {
  const before = stateFor([record("a"), record("b")]);
  let changed = false;
  const unchanged = mockActivity(t, (url) => {
    if (!changed) return { ...page([], null, 1), changes: { updated: 0 } };
    return url.searchParams.has("cursor")
      ? page([record("b")], null, 3)
      : page([record("a")], "next");
  });
  const result = await refreshConversationSnapshot(
    42,
    before,
    () => "b",
    new AbortController().signal,
  );
  assert.equal(result.state.records, before.records);
  assert.equal(result.state.items, before.items);
  assert.equal(unchanged.mock.callCount(), 1);
  changed = true;
  await assert.rejects(
    refreshConversationSnapshot(
      42,
      before,
      () => "b",
      new AbortController().signal,
    ),
    /快照/,
  );
});

test("GitHub and Agent content cannot inject HTML, unsafe links or remote images", () => {
  // Reviews, tool output and identity attributes all carry untrusted repository text.
  const payload =
    '\"><img src=x onerror="alert(1)"><script>alert(2)</script>&\n\n' +
    '<svg onload="alert(3)"></svg>\n\n' +
    "[危险](javascript:alert%281%29)\n\n![图片](https://example.test/track.png)";
  const kinds = [
    "comment",
    "message",
    "tool_call",
    "tool_result",
    "event",
    "check",
  ];
  for (const kind of kinds) {
    const html = renderToStaticMarkup(
      <ActivityView
        entry={
          {
            key: payload,
            version: 1,
            author: payload,
            occurred_at: "2026-10-17T00:00:00Z",
            updated_at: "2026-10-17T00:00:00Z",
            observed_at: "2026-10-17T00:00:00Z",
            source: payload,
            kind,
            title: payload,
            body: payload,
            body_length: payload.length,
            url: null,
            parent_key: null,
            deleted: false,
            prior: false,
            previous_version: null,
            metadata: {
              name: payload,
              workflow_name: payload,
              status: payload,
            },
          } satisfies ActivityItem
        }
      />,
    );
    assert.doesNotMatch(html, /<img|<script|data-entry="">/);
    assert.match(html, /&lt;img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&amp;/);
    assert.match(html, /&quot;&gt;&lt;img/);
    assert.match(html, /&lt;svg onload=/);
    assert.doesNotMatch(html, /href="javascript:|<iframe|<svg[^>]*onload=/);
    assert.doesNotMatch(html, /已编辑|查看历史版本/);
  }
});

test("Markdown destinations use the message source, never the console origin", () => {
  // Archived GitHub comments commonly contain root-, path-, query- and fragment-relative links.
  const body = [
    "[相关](/owner/agentflow/issues/7)",
    "[同级](../pull/98)",
    "[查询](?page=2)",
    "[片段](#issuecomment-456)",
    "[外部](https://example.test/guide)",
    "[邮件](mailto:owner@example.test)",
    "[危险](javascript:alert%281%29)",
    "![相对图片](../assets/status.svg)",
    "![危险图片](javascript:alert%281%29)",
  ].join("\n\n");
  const entry: ActivityItem = {
    key: "source-links",
    version: 1,
    author: "owner",
    occurred_at: "2026-10-20T00:00:00Z",
    updated_at: "2026-10-20T00:00:00Z",
    observed_at: "2026-10-20T00:00:00Z",
    source: "pr.comments",
    kind: "comment",
    title: "评论",
    body,
    body_length: body.length,
    url: "https://github.com/owner/agentflow/issues/85#issuecomment-123",
    parent_key: null,
    deleted: false,
    prior: false,
    previous_version: null,
    metadata: {},
  };
  const html = renderToStaticMarkup(<ActivityView entry={entry} />);
  for (const destination of [
    "https://github.com/owner/agentflow/issues/7",
    "https://github.com/owner/agentflow/pull/98",
    "https://github.com/owner/agentflow/issues/85?page=2",
    "https://github.com/owner/agentflow/issues/85#issuecomment-456",
    "https://github.com/owner/agentflow/assets/status.svg",
    "https://example.test/guide",
    "mailto:owner@example.test",
  ])
    assert.ok(html.includes(`href="${destination}"`), destination);
  assert.doesNotMatch(html, /<img|href="javascript:/);

  for (const url of [null, "invalid source"]) {
    const withoutSource = renderToStaticMarkup(
      <ActivityView entry={{ ...entry, url }} />,
    );
    assert.doesNotMatch(withoutSource, /href="(?:\/|\.\.|\?|#)/);
    assert.match(withoutSource, /href="https:\/\/example.test\/guide"/);
    assert.match(withoutSource, /相关/);
    assert.match(withoutSource, /图片：相对图片/);
  }
});

test("CI checks show workflow, job and status beside the app without hiding summaries", () => {
  // Checks have app identity and job names; completed alone would hide success versus failure.
  const check: ActivityItem = {
    key: "check-1",
    version: 1,
    source: "commit.check_runs:head",
    kind: "check",
    author: "GitHub Actions",
    title: "check (3.10)",
    occurred_at: "2026-10-20T06:49:33Z",
    updated_at: "2026-10-20T06:49:33Z",
    observed_at: "2026-10-20T06:49:33Z",
    body: "检查通过",
    body_length: 4,
    url: null,
    parent_key: null,
    deleted: false,
    prior: false,
    previous_version: null,
    metadata: {
      workflow_name: "CI",
      status: "completed",
      conclusion: "success",
    },
  };
  const html = renderToStaticMarkup(<ActivityView entry={check} />);
  assert.match(
    html,
    /class="event-action">CI: check \(3\.10\): success<\/span>/,
  );
  assert.doesNotMatch(html, /class="message-title"/);
  assert.match(html, /检查通过/);
  for (const [metadata, label] of [
    [
      { workflow_name: "CI", conclusion: "failure" },
      "CI: check (3.10): failure",
    ],
    [{ workflow_name: "CI", status: "queued" }, "CI: check (3.10): queued"],
    [
      { workflow_name: "Release", status: "in_progress", conclusion: "" },
      "Release: check (3.10): in_progress",
    ],
    [
      { status: "completed", conclusion: "cancelled" },
      "未知工作流: check (3.10): cancelled",
    ],
    [{}, "未知工作流: check (3.10): unknown"],
  ] satisfies [ActivityItem["metadata"], string][]) {
    const markup = renderToStaticMarkup(
      <ActivityView entry={{ ...check, metadata }} />,
    );
    assert.ok(markup.includes(`class="event-action">${label}</span>`), label);
  }
});
