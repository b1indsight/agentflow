import assert from "node:assert/strict";
import { test } from "node:test";
import { parseSearch } from "./search";

test("search qualifiers remain exact and malformed queries never broaden server results", () => {
  const parsed = parseSearch(
    'repo:owner/repo status:running pr:87 "worker output"',
    [],
  );
  assert.deepEqual(parsed, {
    ok: true,
    query: {
      repo: "owner/repo",
      status: "running",
      pr: 87,
      terms: ["worker output"],
    },
  });
  for (const q of [
    "repo:",
    "repo:short",
    "is:open",
    "is:done",
    "status:unknown",
    "status:done",
    "status:finalizing",
    "pr:0",
    "pr:1.5",
    "pr:2147483648",
    "pr:1 pr:2",
    "label:bug",
    '"unfinished',
  ]) {
    assert.equal(parseSearch(q, []).ok, false, q);
  }
  // The archived column is a business lifecycle, not the old display-only done bucket.
  assert.deepEqual(parseSearch("status:archived", []), {
    ok: true,
    query: { terms: [], status: "archived" },
  });
  for (const lifecycle of ["active", "archived"]) {
    assert.deepEqual(parseSearch(`is:${lifecycle}`, []), {
      ok: true,
      query: { terms: [], lifecycle },
    });
  }
});
