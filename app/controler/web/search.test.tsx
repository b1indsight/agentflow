import assert from "node:assert/strict";
import { test } from "node:test";
import { flows } from "./fixtures";
import { matchesFlow, parseSearch } from "./search";

test("a submitted query combines exact qualifiers and case-insensitive words without broadening malformed input", () => {
  const repos = [...new Set(flows.map((flow) => flow.repo))];
  const results = (raw: string) => {
    const result = parseSearch(raw, repos);
    assert.ok(result.ok, raw);
    return flows
      .filter((flow) => matchesFlow(flow, result.query))
      .map((flow) => flow.id);
  };
  assert.deepEqual(
    results(
      'repo:b1indsight/agentflow is:active status:running issue:82 pr:95 WEBHOOK "退避重试"',
    ),
    [42],
  );
  assert.deepEqual(results("pr:9"), []);
  assert.deepEqual(results("is:done"), [39]);
  assert.deepEqual(results("status:queued 部署"), [37]);
  assert.equal(results("").length, flows.length);
  // A half-written qualifier, typo or duplicate must keep the last applied query in the UI.
  for (const raw of [
    "repo:",
    "is:open",
    "status:unknown",
    "pr:0",
    "pr:1.5",
    "issue:9007199254740992",
    "label:bug",
    '"unfinished',
    "pr:95 pr:97",
    "repo:unknown",
  ]) {
    const parsed = parseSearch(raw, repos);
    assert.equal(parsed.ok, false, raw);
    if (!parsed.ok) assert.ok(parsed.error.length > 0);
  }
});
