import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ActivityView } from "./activity";
import type { ActivityItem } from "./types";

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
