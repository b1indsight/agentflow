import { expect, test } from "@playwright/test";

const now = "2026-10-17T12:00:00Z";
const flow = {
  id: 42,
  repo: "owner/agentflow",
  issue_number: 85,
  issue_url: "https://example.test/issues/85",
  title: "真实 DTO Flow",
  pr_number: 87,
  pr_url: "https://example.test/pulls/87",
  lifecycle: "active",
  phase: "running",
  action: "正在验证",
  worker_id: "worker-1",
  worker_historical: false,
  revision_cycles: 1,
  branch: "flow/85",
  created_at: now,
  updated_at: now,
  state_version: 2,
};
const activity = {
  key: "comment-1",
  version: 2,
  source: "github",
  kind: "comment",
  occurred_at: now,
  updated_at: now,
  observed_at: now,
  author: "owner",
  title: "评论",
  body: "真实活动正文",
  body_length: 6,
  url: null,
  parent_key: null,
  deleted: false,
  prior: false,
  previous_version: 1,
  metadata: {},
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const pageOf = (items: unknown[]) => ({
      items,
      next_cursor: null,
      has_more: false,
    });
    if (url.pathname === "/api/repos")
      return route.fulfill({ json: pageOf([flow.repo]) });
    if (url.pathname === "/api/flows")
      return route.fulfill({
        json: {
          ...pageOf(url.searchParams.get("phase") === "running" ? [flow] : []),
          counts: { queued: 0, running: 1, review: 0, blocked: 0, done: 0 },
        },
      });
    if (url.pathname === "/api/flows/42")
      return route.fulfill({
        json: {
          ...flow,
          reason: "真实原因",
          next_condition: "真实条件",
          sources: [],
        },
      });
    if (url.pathname.endsWith("/jobs"))
      return route.fulfill({ json: pageOf([]) });
    if (url.pathname.endsWith("/activity"))
      return route.fulfill({
        json: {
          ...pageOf([activity]),
          snapshot: 2,
          total: 1,
          sources: [],
          changes: {},
        },
      });
    if (
      url.pathname.endsWith("/content") &&
      url.searchParams.get("compare") === "true"
    )
      return route.fulfill({
        json: {
          key: activity.key,
          version: 2,
          previous_version: 1,
          text: "--- 上一版\n+++ 当前版本\n@@ -1 +1 @@\n-原来的活动正文\n+真实活动正文",
          next_offset: null,
          total: 59,
        },
      });
    return route.fulfill({ status: 500, json: { detail: "模拟失败" } });
  });
});

test("uses console DTOs and a collapsible edited hint with the previous-version diff", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".card[data-flow='42']").first()).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-board.png",
    fullPage: true,
  });
  await page.locator(".card[data-flow='42']").first().click();
  await expect(
    page.getByRole("heading", { name: "真实 DTO Flow" }),
  ).toBeVisible();
  await page.goto("/#flow/42/conversation");
  await expect(page.getByText("真实活动正文")).toBeVisible();
  const edited = page.getByRole("button", { name: "已编辑", exact: true });
  await expect(edited).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("button", { name: "查看历史版本" })).toHaveCount(
    0,
  );
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-edited-collapsed.png",
    fullPage: true,
  });
  await edited.focus();
  await page.keyboard.press("Enter");
  const diff = page.getByRole("region", { name: "与上一版的差异" });
  await expect(diff.locator(".diff-code-delete")).toHaveText("原来的活动正文");
  await expect(diff.locator(".diff-code-insert")).toHaveText("真实活动正文");
  await expect(diff.locator(".diff-code-edit").first()).toBeVisible();
  await expect(edited).toHaveAttribute("aria-expanded", "true");
  expect(
    await page
      .locator("[data-window-list]")
      .evaluateAll((lists) =>
        lists.every(
          (list) =>
            list.querySelectorAll(":scope > [data-window-row]").length <= 60,
        ),
      ),
  ).toBe(true);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-edited-diff.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(diff).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-edited-mobile.png",
    fullPage: true,
  });
  await edited.click();
  await expect(diff).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("tab", { name: "概览" }).click();
  await expect(page.getByRole("heading", { name: "任务信息" })).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-overview.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByText("更新", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-overview-times-mobile.png",
    fullPage: true,
  });
});

test("reads a complete patch once and windows parsed rows without losing line numbers or text safety", async ({
  page,
}) => {
  // One large hunk crosses both the former 16K response boundary and several DOM windows.
  const rows = Array.from(
    { length: 450 },
    (_, i) => `第 ${i + 1} 行正文，完整保留这次观察到的内容。`,
  );
  const text = [
    "--- 上一版",
    "+++ 当前版本",
    "@@ -1,450 +1,450 @@",
    ...rows.map((s) => `-${s}`),
    ...rows.map((s) => `+更新后的${s}`),
    "@@ -900 +900 @@",
    '-<script>alert("old")</script>',
    '+<img src=x onerror="alert(1)">',
  ].join("\n");
  expect(text.length).toBeGreaterThan(16384);
  let requests = 0;
  await page.route("**/api/flows/42/activity/content?**", async (route) => {
    requests++;
    const url = new URL(route.request().url());
    expect(url.searchParams.has("offset")).toBe(false);
    expect(url.searchParams.has("limit")).toBe(false);
    await route.fulfill({
      json: {
        key: activity.key,
        version: 2,
        previous_version: 1,
        text,
        next_offset: null,
        total: text.length,
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const edited = page.getByRole("button", { name: "已编辑", exact: true });
  await edited.click();
  const diff = page.getByRole("region", { name: "与上一版的差异" });
  const next = diff.getByRole("button", { name: "下一段差异" });
  await expect(diff.locator(".diff-line")).toHaveCount(200);
  await expect(diff.locator(".diff-gutter-delete").first()).toHaveText("1");
  expect(
    await diff.locator(".diff-window").evaluate((node) => node.clientHeight),
  ).toBeLessThanOrEqual(480);
  await diff.screenshot({
    path: "../../../.amp/in/artifacts/console-diff-long.png",
  });
  for (let i = 0; i < 4; i++) {
    await next.click();
    expect(await diff.locator(".diff-line").count()).toBeLessThanOrEqual(200);
  }
  await expect(diff.locator(".diff-code-insert").last()).toHaveText(
    '<img src=x onerror="alert(1)">',
  );
  await expect(diff.locator(".diff-gutter-insert").last()).toHaveText("900");
  await expect(diff.locator("script, img")).toHaveCount(0);
  await expect(next).toBeDisabled();
  await diff.getByRole("button", { name: "上一段差异" }).click();
  const firstNumber = diff
    .locator(".diff-line")
    .first()
    .locator(".diff-gutter-insert")
    .last();
  await expect(firstNumber).toHaveText("151");
  await edited.click();
  await edited.click();
  await expect(firstNumber).toHaveText("151");
  expect(requests).toBe(1);
});

test("surfaces API failures without replacing existing UI", async ({
  page,
}) => {
  await page.route("**/api/flows?**", (route) =>
    route.fulfill({ status: 503, json: { detail: "服务暂不可用" } }),
  );
  await page.goto("/");
  await expect(
    page
      .getByRole("button", { name: "读取失败：服务暂不可用（重试）" })
      .first(),
  ).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-error.png",
    fullPage: true,
  });
});

test("large activity stays windowed, re-reads evicted pages, and refreshes explicitly", async ({
  page,
}) => {
  let refreshed = false;
  const rereads: string[] = [],
    errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/flows/42/activity?**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("refresh") === "true") refreshed = true;
    const start = Number(url.searchParams.get("cursor") ?? 0);
    rereads.push(`${start}:${url.searchParams.get("snapshot")}`);
    const items = Array.from({ length: Math.min(20, 320 - start) }, (_, i) => ({
      ...activity,
      key: `large-${start + i}`,
      version: refreshed ? 3 : 2,
      body: `归档正文 ${start + i}`,
      body_length: 20,
    }));
    return route.fulfill({
      json: {
        items,
        snapshot: refreshed ? 3 : 2,
        total: 320,
        next_cursor: start + 20 < 320 ? String(start + 20) : null,
        has_more: start + 20 < 320,
        sources: [],
        changes: refreshed ? { edited: 1 } : {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  await expect(page.getByText("归档正文 0", { exact: true })).toBeVisible();
  const root = page.locator(".detail-body");
  const head = await page.locator(".detail-head").boundingBox();
  await expect
    .poll(
      async () => {
        await root.evaluate((x) => {
          x.scrollTop = x.scrollHeight;
        });
        return page
          .locator('[data-window-list="会话活动"]')
          .getAttribute("data-window-count");
      },
      { timeout: 15000 },
    )
    .toBe("320");
  await root.evaluate((x) => {
    x.scrollTop = 0;
  });
  await expect(page.getByText("归档正文 0", { exact: true })).toBeVisible();
  expect(rereads.filter((x) => x === "0:2").length).toBeGreaterThan(0);
  await expect(page.locator('[data-window-list="会话活动"]')).toHaveAttribute(
    "data-window-count",
    "320",
  );
  expect(
    await page.locator(".conversation-stream [data-window-row]").count(),
  ).toBeLessThanOrEqual(60);
  expect((await page.locator(".detail-head").boundingBox())?.y).toBe(head?.y);
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "快照更新" }),
  ).toBeVisible();
  expect(refreshed).toBe(true);
  expect(errors).toEqual([]);
});

test("thread replies and body continuation use the pinned snapshot and Unicode offsets", async ({
  page,
}) => {
  const thread = { ...activity, kind: "thread", body: "😀", body_length: 3 };
  await page.route("**/api/flows/42/activity?**", async (route) => {
    const url = new URL(route.request().url());
    const parent = url.searchParams.get("parent");
    if (parent) expect(url.searchParams.get("snapshot")).toBe("2");
    return route.fulfill({
      json: {
        items: parent
          ? [{ ...activity, key: "reply", body: "线程回复正文" }]
          : [thread],
        snapshot: 2,
        total: 1,
        has_more: false,
        next_cursor: null,
        sources: [],
        changes: {},
      },
    });
  });
  await page.route("**/api/flows/42/activity/content?**", (route) => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("offset")).toBe("1");
    expect(url.searchParams.get("version")).toBe("2");
    return route.fulfill({
      json: {
        key: thread.key,
        version: 2,
        text: "后文",
        next_offset: null,
        total: 3,
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  await page.getByRole("button", { name: "下一部分" }).click();
  await expect(page.getByText("后文", { exact: true })).toBeVisible();
  // Tab changes unmount the activity row, just as virtual-window eviction does.
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(page.getByText("后文", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看线程回复" }).click();
  await expect(page.getByText("线程回复正文", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "切换任务" }).click();
  await expect(page.getByRole("dialog", { name: "Flow 导航" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-mobile.png",
    fullPage: true,
  });
});
