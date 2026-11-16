import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionCache } from "./sessionCache";

test("byte pressure evicts least-recently-used entries before the count limit", () => {
  // A few large responses must not bypass the bound merely because there are few keys.
  const cache = new SessionCache<string, string>(
    4,
    10,
    (_, value) => value.length,
  );
  cache.set("a", "1234");
  cache.set("b", "5678");
  assert.equal(cache.get("a"), "1234");
  cache.set("c", "9012");
  assert.equal(cache.get("b"), undefined);
  assert.equal(cache.get("a"), "1234");
  cache.set("d", "1234567890");
  assert.equal(cache.get("a"), undefined);
  assert.equal(cache.get("c"), undefined);
  assert.equal(cache.get("d"), "1234567890");
});

test("replacement frees its old weight and oversized values leave no stale entry", () => {
  const cache = new SessionCache<string, string>(
    4,
    10,
    (_, value) => value.length,
  );
  cache.set("a", "12345678");
  cache.set("b", "12");
  cache.set("a", "12");
  cache.set("c", "123456");
  assert.equal(cache.get("b"), "12");
  assert.equal(cache.get("a"), "12");
  // A refreshed snapshot may outgrow the budget: do not resurrect its previous cached snapshot.
  cache.set("a", "12345678901");
  cache.set("oversized", "12345678901");
  assert.equal(cache.get("a"), undefined);
  assert.equal(cache.get("oversized"), undefined);
  assert.equal(cache.get("b"), "12");
  assert.equal(cache.get("c"), "123456");
  cache.set("d", "12");
  assert.equal(cache.get("b"), "12");
  assert.equal(cache.get("c"), "123456");
  assert.equal(cache.get("d"), "12");
});
