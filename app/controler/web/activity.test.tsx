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
    "[危险](javascript:alert%281%29)\n\n![图片](https://example.test/track.png)";
  const kinds = ["comment", "message", "tool_call", "tool_result", "event"];
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
            metadata: { name: payload },
          } satisfies ActivityItem
        }
      />,
    );
    assert.doesNotMatch(html, /<img|<script|data-entry="">/);
    assert.match(html, /&lt;img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&amp;/);
    assert.match(html, /&quot;&gt;&lt;img/);
    assert.doesNotMatch(html, /href="javascript:|<iframe|<svg/);
    assert.doesNotMatch(html, /已编辑|查看历史版本/);
  }
});
