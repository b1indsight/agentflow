import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ActivityView } from "./activity";
import type { Activity, Block } from "./types";

test("GitHub and Agent content remains text in every activity surface", () => {
  // Reviews, tool output and identity attributes all carry untrusted repository text.
  const payload = '\"><img src=x onerror="alert(1)"><script>alert(2)</script>&';
  const blocks: Block[] = [
    { kind: "paragraph", text: payload },
    { kind: "heading", text: payload },
    { kind: "verdict", text: payload },
    { kind: "tool", name: payload, output: payload, status: payload },
    {
      kind: "thread",
      path: payload,
      lines: payload,
      code: payload,
      resolved: false,
      replies: [{ author: payload, time: payload, text: payload }],
    },
  ];
  const kinds: Activity["kind"][] = ["github", "agent", "event"];
  for (const kind of kinds) {
    const html = renderToStaticMarkup(
      <ActivityView
        entry={{
          id: payload,
          author: payload,
          time: payload,
          source: payload,
          kind,
          blocks,
        }}
      />,
    );
    assert.doesNotMatch(html, /<img|<script|data-entry="">/);
    assert.match(html, /&lt;img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&amp;/);
    assert.match(html, /<pre[^>]*><code>&quot;&gt;&lt;img/);
  }
});
