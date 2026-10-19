import { expect, test } from "@playwright/test";
import type { SourceStatus } from "../types";

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

test("archived column distinguishes closed unmerged PRs from completed archives", async ({
  page,
}) => {
  const closed = {
    ...flow,
    title: "已关闭的 PR",
    phase: "done",
    action: "delivery_closed",
  };
  const archived = {
    ...closed,
    id: 43,
    title: "正常归档的 PR",
    lifecycle: "succeeded",
    action: "archived",
  };
  await page.route("**/api/flows?**", (route) =>
    route.fulfill({
      json: {
        items:
          new URL(route.request().url()).searchParams.get("phase") === "done"
            ? [closed, archived]
            : [],
        counts: { queued: 0, running: 0, review: 0, blocked: 0, done: 2 },
        next_cursor: null,
        has_more: false,
      },
    }),
  );
  await page.route("**/api/flows/42", (route) =>
    route.fulfill({
      json: {
        ...closed,
        reason: "PR 已关闭（未合并），编码与修订已暂停",
        next_condition: "重新打开 PR 并完成执行状态及 head/base 对账后才能继续",
        sources: [],
      },
    }),
  );
  await page.goto("/#board");
  const column = page.locator(".column.phase-done");
  await expect(column.locator(".col-header")).toHaveText("已归档2");
  await expect(
    column.getByText("已关闭 · 未合并", { exact: true }),
  ).toBeVisible();
  await expect(column.getByText("归档完成", { exact: true })).toBeVisible();
  await expect(page.getByText("已完成", { exact: true })).toHaveCount(0);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/archived-board-desktop.png",
  });
  const search = page.getByRole("combobox", { name: "搜索任务与筛选条件" });
  await search.fill("is:d");
  await expect(page.getByRole("option", { name: /已归档/ })).toBeVisible();
  await search.fill("");
  await search.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .locator(".mobile-stages")
    .getByRole("button", { name: /已归档/ })
    .click();
  await expect(
    column.getByText("已关闭 · 未合并", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/archived-board-mobile.png",
  });
  await column.locator('.card[data-flow="42"]').click();
  await expect(page.locator(".detail-head .badge")).toHaveText(
    "已关闭 · 未合并",
  );
  await page.getByRole("tab", { name: "概览" }).click();
  await expect(
    page.getByText("active / 已归档", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/archived-detail-mobile.png",
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({
    path: "../../../.amp/in/artifacts/archived-detail-desktop.png",
  });
});

test("conversation sources keep routine diagnostics collapsed and surface actionable failures", async ({
  page,
}) => {
  // Historical coverage limits and a not-yet-created worker session are normal
  // on a new flow; an actual read failure must still be visible and retryable.
  const historyLimit = "pre_ingestion_edits_and_deleted_bodies_unrecoverable";
  let sources: SourceStatus[] = [
    {
      source: "github.issue_comments",
      status: "partial",
      reason: null,
      last_success_at: now,
      limitations: [historyLimit],
    },
    {
      source: "worker",
      status: "available",
      reason: null,
      last_success_at: now,
      limitations: [],
    },
    {
      source: "pr.commits",
      status: "partial",
      reason: null,
      last_success_at: now,
      limitations: ["github_pr_commit_list_maximum_250"],
    },
    {
      source: "pr.review_threads",
      status: "partial",
      reason: null,
      last_success_at: now,
      limitations: ["thread_state_has_no_remote_change_timestamp"],
    },
  ];
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items: [activity],
        next_cursor: null,
        has_more: false,
        snapshot: 2,
        total: 1,
        sources,
        changes: {},
      },
    }),
  );
  await page.goto("/#flow/42/conversation");
  const status = page.getByRole("region", { name: "会话数据状态" });
  const details = status.locator("details");
  const warning = status.getByRole("alert");
  const refresh = page.getByRole("button", { name: "刷新当前视图" });
  await expect(status).toBeVisible();
  await expect(details).not.toHaveAttribute("open");
  await expect(status.getByText(historyLimit, { exact: true })).toBeHidden();
  await expect(warning).toHaveCount(0);
  await details.locator("summary").click();
  await expect(
    status.getByText("github_pr_commit_list_maximum_250", { exact: true }),
  ).toBeVisible();
  await expect(
    status.getByText("thread_state_has_no_remote_change_timestamp", {
      exact: true,
    }),
  ).toBeVisible();
  await details.locator("summary").click();
  await status.screenshot({
    path: "../../../.amp/in/artifacts/closed-pr-sources-normal.png",
  });

  sources = [
    ...sources,
    {
      source: "issue.timeline",
      status: "unavailable",
      reason: "unexpected_response",
      last_success_at: null,
      limitations: [],
    },
  ];
  await refresh.click();
  await expect(warning).toContainText("issue.timeline：同步异常");
  await expect(warning).not.toContainText("pr.commits");
  await expect(warning).not.toContainText("pr.review_threads");
  await warning.screenshot({
    path: "../../../.amp/in/artifacts/closed-pr-sources-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await warning.screenshot({
    path: "../../../.amp/in/artifacts/closed-pr-sources-mobile.png",
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  sources = [
    sources[0],
    { ...sources[1], status: "unavailable", reason: "no_session" },
  ];
  await refresh.click();
  await expect(status.getByRole("status")).toHaveText("尚无执行会话");
  await expect(warning).toHaveCount(0);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/sources-empty.png",
  });
  sources = [sources[0], { ...sources[1], reason: "no_worker" }];
  await refresh.click();
  await expect(status.getByRole("status")).toHaveText(
    "尚未绑定 Worker，暂不读取执行会话",
  );
  await expect(warning).toHaveCount(0);

  sources = [sources[0], { ...sources[1], reason: "query_timeout" }];
  await refresh.click();
  await expect(warning).toContainText("执行会话：会话读取超时");
  await expect(page.getByText("真实活动正文", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/sources-warning.png",
  });
  await details.locator("summary").click();
  await expect(status.getByText(historyLimit, { exact: true })).toBeVisible();
  await expect(status.locator("time").first()).toHaveAttribute("datetime", now);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/sources-details-mobile.png",
    fullPage: true,
  });

  sources = sources.map((source) => ({
    ...source,
    status: "available",
    reason: null,
    limitations: [],
  }));
  await warning.getByRole("button", { name: "重新同步" }).click();
  await expect(warning).toHaveCount(0);
  await expect(status.getByRole("status")).toHaveCount(0);

  // Unexplained or newly introduced partial coverage must not be hidden.
  for (const limitations of [[], ["new_unknown_limitation"]]) {
    sources = [{ ...sources[0], status: "partial", limitations }];
    await refresh.click();
    await expect(warning).toContainText("内容采集不完整");
  }
});

test("activity content hierarchy keeps provenance secondary and expandable", async ({
  page,
}) => {
  const description = {
    ...activity,
    author: "b1indsight",
    source: "issue.description",
    title: "测试 agentflow 是否正常运行",
    body: "如果能正常响应就返回 ok",
    body_length: Array.from("如果能正常响应就返回 ok").length,
    prior: true,
    previous_version: null,
    url: "https://example.test/issues/85",
    metadata: { issue_number: 85, repository: "b1indsight/agentflow" },
  };
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items: [description],
        next_cursor: null,
        has_more: false,
        snapshot: 2,
        total: 1,
        sources: [],
        changes: {},
      },
    }),
  );
  await page.goto("/#flow/42/conversation");
  const message = page.locator("article[data-entry='comment-1']");
  await expect(message.getByText("此前交互", { exact: true })).toBeVisible();
  await expect(message.getByText("此前交互", { exact: true })).toHaveAttribute(
    "title",
    "发生于此 Flow 创建之前的历史内容",
  );
  await expect(
    message.getByRole("heading", { name: description.title }),
  ).toBeVisible();
  await expect(
    message.getByText(description.body, { exact: true }),
  ).toBeVisible();
  await expect(
    message.getByRole("link", { name: "在 GitHub 中查看" }),
  ).toHaveAttribute("href", description.url);
  const metadata = message.locator("details");
  await expect(metadata).not.toHaveAttribute("open");
  await page.locator(".conversation-stream").screenshot({
    path: "../../../.amp/in/artifacts/activity-hierarchy-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".conversation-stream").screenshot({
    path: "../../../.amp/in/artifacts/activity-hierarchy-mobile.png",
  });
  await metadata.locator("summary").click();
  await expect(metadata.getByLabel("文本")).toContainText("issue_number");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.locator(".conversation-stream").screenshot({
    path: "../../../.amp/in/artifacts/activity-hierarchy-details-mobile.png",
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
  await page.getByRole("button", { name: "下一段", exact: true }).click();
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

test("refresh resets reply cursors even when the thread version is unchanged", async ({
  page,
}) => {
  let snapshot = 2;
  const reads: string[] = [];
  await page.route("**/api/flows/42/activity?**", async (route) => {
    const url = new URL(route.request().url());
    const parent = url.searchParams.get("parent");
    if (url.searchParams.get("refresh") === "true") snapshot = 3;
    if (parent) {
      const cursor = url.searchParams.get("cursor");
      const pinned = url.searchParams.get("snapshot");
      reads.push(`${cursor}:${pinned}`);
      if (cursor && cursor !== `reply-${pinned}`)
        return route.fulfill({
          status: 422,
          json: { detail: "cursor_invalid" },
        });
      return route.fulfill({
        json: {
          items: [
            {
              ...activity,
              key: `reply-${pinned}`,
              body: `快照 ${pinned} 回复`,
            },
          ],
          snapshot,
          total: 2,
          has_more: true,
          next_cursor: `reply-${pinned}`,
          sources: [],
          changes: {},
        },
      });
    }
    return route.fulfill({
      json: {
        items: [{ ...activity, kind: "thread" }],
        snapshot,
        total: 1,
        has_more: false,
        next_cursor: null,
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  await page.getByRole("button", { name: "查看线程回复" }).click();
  await expect(page.getByText("快照 2 回复", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(
    page.getByRole("button", { name: "查看线程回复" }),
  ).toBeVisible();
  await expect(page.getByText("快照 2 回复", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查看线程回复" }).click();
  await expect(page.getByText("快照 3 回复", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "下一页线程回复" }).click();
  await expect.poll(() => reads).toEqual(["null:2", "null:3", "reply-3:3"]);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("cached later-page errors retain a working retry after changing tabs", async ({
  page,
}) => {
  const cursors: string[] = [];
  let failures = 1;
  await page.route("**/api/flows/42/activity?**", async (route) => {
    const url = new URL(route.request().url());
    const cursor = url.searchParams.get("cursor");
    if (cursor) {
      cursors.push(`${cursor}:${url.searchParams.get("snapshot")}`);
      if (failures-- > 0) return route.fulfill({ status: 502, body: "" });
    }
    return route.fulfill({
      json: {
        items: [
          {
            ...activity,
            key: cursor ? "later" : "first",
            body: cursor ? "后页正文" : "首页正文",
          },
        ],
        snapshot: 2,
        total: 2,
        has_more: !cursor,
        next_cursor: cursor ? null : "page-2",
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const retry = page.getByRole("button", {
    name: "读取失败：请求失败 (502)（重试）",
  });
  await expect(retry).toBeVisible();
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(page.getByText("后页正文", { exact: true })).toBeVisible();
  expect(cursors).toEqual(["page-2:2", "page-2:2"]);
  await expect(retry).toHaveCount(0);
});

test("body navigation has one control and continues across preview boundaries", async ({
  page,
}) => {
  // Real archive previews stop at 2000 code points, before the local 2048-character window.
  const body = "首".repeat(2000) + "😀".repeat(2500) + "末尾";
  const points = Array.from(body);
  let fail = true;
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...activity,
            previous_version: null,
            body: points.slice(0, 2000).join(""),
            body_length: points.length,
          },
        ],
        snapshot: 2,
        total: 1,
        has_more: false,
        next_cursor: null,
        sources: [],
        changes: {},
      },
    }),
  );
  await page.route("**/api/flows/42/activity/content?**", (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get("offset"));
    const limit = Number(url.searchParams.get("limit"));
    if (fail) {
      fail = false;
      return route.fulfill({ status: 502, body: "" });
    }
    const text = points.slice(offset, offset + limit).join("");
    return route.fulfill({
      json: {
        key: activity.key,
        version: 2,
        text,
        total: points.length,
        next_offset:
          offset + Array.from(text).length < points.length
            ? offset + Array.from(text).length
            : null,
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const row = page.locator("[data-entry='comment-1']");
  const next = row.getByRole("button", { name: "下一段", exact: true });
  await expect(next).toHaveCount(1);
  await next.click();
  await expect(row.getByRole("alert")).toBeVisible();
  await expect(row.locator(".message-body")).toHaveText("首".repeat(2000));
  await row.getByRole("alert").screenshot({
    path: "../../../.amp/in/artifacts/console-fixes-body-error.png",
  });
  await row.getByRole("button", { name: "重试正文读取" }).click();
  await expect(row.locator(".message-body")).toHaveText("😀".repeat(1024));
  await expect(next).toHaveCount(1);
  await row.getByRole("group", { name: "正文分段浏览" }).screenshot({
    path: "../../../.amp/in/artifacts/console-fixes-body-controls.png",
  });
  await row.getByRole("button", { name: "上一段", exact: true }).click();
  await expect(row.locator(".message-body")).toHaveText("首".repeat(2000));
  for (let i = 0; i < 4; i++) {
    await next.click();
    await expect(
      row.getByRole("group", { name: "正文分段浏览" }),
    ).toHaveAttribute("aria-busy", "false");
    if (await next.isDisabled()) break;
  }
  await expect(row.locator(".message-body")).toContainText("末尾");
  await expect(next).toBeDisabled();
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(row.locator(".message-body")).toContainText("末尾");
});

test("assistant tools share one compact activity bar across pages without crossing turns", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const context = {
    attempt_id: 1,
    session_id: "session-a",
    session_generation: 0,
  };
  const worker = {
    ...activity,
    source: "worker",
    previous_version: null,
    metadata: context,
  };
  const items = [
    {
      ...activity,
      key: "label",
      source: "issue.timeline",
      kind: "event",
      title: "labeled",
      body: "",
      body_length: 0,
      previous_version: null,
      metadata: { event: "labeled", label: "flow" },
    },
    {
      ...worker,
      key: "message",
      author: "assistant",
      kind: "message",
      title: "message",
      body: "我会先检查仓库，再运行验证。",
      body_length: 14,
    },
    {
      ...worker,
      key: "call-a",
      author: "assistant",
      kind: "tool_call",
      title: "exec",
      body: "git status --short",
      body_length: 18,
      metadata: { ...context, call_id: "call-a" },
    },
    {
      ...worker,
      key: "call-b",
      author: "assistant",
      kind: "tool_call",
      title: "test",
      body: "pytest",
      body_length: 6,
      metadata: { ...context, call_id: "call-b" },
    },
    {
      ...worker,
      key: "result-b",
      author: "tool",
      kind: "tool_result",
      title: "tool_result",
      body: "测试通过",
      body_length: 4,
      metadata: { ...context, call_id: "call-b" },
    },
    {
      ...worker,
      key: "result-a",
      author: "tool",
      kind: "tool_result",
      title: "tool_result",
      body: "工作目录干净",
      body_length: 6,
      metadata: { ...context, call_id: "call-a" },
    },
    {
      ...worker,
      key: "answer",
      author: "assistant",
      kind: "message",
      title: "message",
      body: "验证完成，下面是修复结果。",
      body_length: 13,
    },
    {
      ...worker,
      key: "orphan",
      author: "tool",
      kind: "tool_result",
      title: "tool_result",
      body: "其他会话的结果",
      body_length: 7,
      metadata: { ...context, session_id: "session-b", call_id: "call-a" },
    },
    {
      ...worker,
      key: "missing-id",
      author: "tool",
      kind: "tool_result",
      title: "tool_result",
      body: "没有调用编号的结果",
      body_length: 9,
      metadata: { ...context, session_id: "session-b", call_id: "" },
    },
    {
      ...worker,
      key: "user-turn",
      author: "user",
      kind: "message",
      title: "message",
      body: "继续",
      body_length: 2,
    },
    {
      ...worker,
      key: "next-call",
      author: "assistant",
      kind: "tool_call",
      title: "exec",
      body: "pwd",
      body_length: 3,
      metadata: { ...context, call_id: "call-a" },
    },
    {
      ...worker,
      key: "next-result",
      author: "tool",
      kind: "tool_result",
      title: "tool_result",
      body: "下一轮的目录",
      body_length: 6,
      metadata: { ...context, call_id: "call-a" },
    },
  ];
  await page.route("**/api/flows/42/activity?**", (route) => {
    const later = new URL(route.request().url()).searchParams.has("cursor");
    return route.fulfill({
      json: {
        items: later ? items.slice(4) : items.slice(0, 4),
        snapshot: 2,
        total: items.length,
        has_more: !later,
        next_cursor: later ? null : "tools",
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  await expect(page.locator("[data-entry='orphan']")).toBeAttached();
  await expect(page.getByText("添加标签：flow", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /^(message|tool_result|exec|labeled)$/ }),
  ).toHaveCount(0);
  await expect(page.locator("[data-entry='call-a']")).toHaveClass(
    /continuation/,
  );
  await expect(page.locator("[data-entry='orphan']")).not.toHaveClass(
    /continuation/,
  );
  await expect(
    page.getByText("工作目录干净", { exact: true }),
  ).not.toBeVisible();
  const tools = page.getByRole("button", {
    name: "工具活动 · 2 次调用 · 2 条结果",
    exact: true,
  });
  await expect(tools).toHaveCount(1);
  await expect(
    page.getByText("验证完成，下面是修复结果。", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("[data-entry='answer']")).toHaveClass(
    /continuation/,
  );
  await expect(
    page.getByText("下一轮的目录", { exact: true }),
  ).not.toBeVisible();
  // Capture this turn only; later fixture rows deliberately exercise unrelated sessions/turns.
  const captureOutput = async (name: string) => {
    const first = await page.locator("[data-entry='message']").boundingBox();
    const last = await page.locator("[data-entry='answer']").boundingBox();
    if (!first || !last) throw new Error("Assistant output is not mounted");
    await page.screenshot({
      path: `../../../.amp/in/artifacts/tool-bar-${name}.png`,
      clip: {
        x: first.x,
        y: first.y,
        width: first.width,
        height: last.y + last.height - first.y,
      },
    });
  };
  await captureOutput("desktop-collapsed");
  await tools.click();
  await expect(page.getByText("工作目录干净", { exact: true })).toBeVisible();
  await expect(page.getByText("测试通过", { exact: true })).toBeVisible();
  await expect(
    page.getByText("其他会话的结果", { exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByText("下一轮的目录", { exact: true }),
  ).not.toBeVisible();
  await expect(page.locator(".tool-activity-body pre")).toHaveCount(4);
  const callA = page.locator("article[data-entry='call-a']");
  const callB = page.locator("article[data-entry='call-b']");
  await expect(callA.getByText("工作目录干净", { exact: true })).toBeVisible();
  await expect(callB.getByText("测试通过", { exact: true })).toBeVisible();
  await expect(callA.locator("pre")).toHaveText([
    "exec git status --short",
    "工作目录干净",
  ]);
  await expect(callB.locator("pre")).toHaveText(["test pytest", "测试通过"]);
  await expect(callA.locator("blockquote")).toHaveText("工作目录干净");
  await expect(callB.locator("blockquote")).toHaveText("测试通过");
  await expect(page.locator(".tool-activity-body small")).toHaveCount(0);
  await expect(callA.locator(".tool-activity-body")).not.toContainText(
    "call-a",
  );
  await expect(callB.locator(".tool-activity-body")).not.toContainText(
    "call-b",
  );
  await expect(
    page.locator("article[data-entry='result-a']"),
  ).not.toBeVisible();
  await expect(
    page.locator("article[data-entry='result-b']"),
  ).not.toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1100 });
  await captureOutput("desktop-expanded");
  await page.setViewportSize({ width: 390, height: 844 });
  await tools.click();
  await expect(tools).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByText("验证完成，下面是修复结果。", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await captureOutput("mobile-collapsed");
  await tools.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByText("工作目录干净", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 1100 });
  await captureOutput("mobile-expanded");
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(page.getByText("工作目录干净", { exact: true })).toBeVisible();
  await expect(page.getByText("测试通过", { exact: true })).toBeVisible();
  await tools.click();
  await expect(
    page.getByText("工作目录干净", { exact: true }),
  ).not.toBeVisible();
  await expect(page.getByText("测试通过", { exact: true })).not.toBeVisible();
  await page
    .getByRole("button", {
      name: "工具活动 · 1 次调用 · 1 条结果",
      exact: true,
    })
    .click();
  await expect(page.getByText("下一轮的目录", { exact: true })).toBeVisible();
  await expect(
    page
      .locator("article[data-entry='next-call']")
      .getByText("下一轮的目录", { exact: true }),
  ).toBeVisible();
  await page
    .locator("[data-entry='orphan']")
    .getByRole("button", { name: /^工具活动/ })
    .click();
  await expect(
    page
      .locator("article[data-entry='orphan']")
      .getByText("其他会话的结果", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator("article[data-entry='missing-id']")
      .getByText("没有调用编号的结果", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("工作目录干净", { exact: true }),
  ).not.toBeVisible();
  expect(errors).toEqual([]);
});

test("long tool activity remains bounded and readable across virtual windows", async ({
  page,
}) => {
  // A long uninterrupted tool run must not bypass the raw-row mounting budget when opened.
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/flows/42/activity?**", (route) => {
    const start = Number(
      new URL(route.request().url()).searchParams.get("cursor") ?? 0,
    );
    return route.fulfill({
      json: {
        items: Array.from({ length: Math.min(20, 141 - start) }, (_, index) => {
          const n = start + index;
          const body = n === 140 ? "本轮全部完成" : `工具记录 ${n}`;
          return {
            ...activity,
            key: `tool-run-${n}`,
            source: "worker",
            author: n % 2 ? "tool" : "assistant",
            kind: n === 140 ? "message" : n % 2 ? "tool_result" : "tool_call",
            title: n === 140 ? "message" : n % 2 ? "tool_result" : "exec",
            body,
            body_length: body.length,
            previous_version: null,
            metadata: {
              attempt_id: 1,
              session_id: "long-tools",
              session_generation: 0,
              call_id: `call-${Math.floor(n / 2)}`,
            },
          };
        }),
        snapshot: 2,
        total: 141,
        next_cursor: start + 20 < 141 ? String(start + 20) : null,
        has_more: start + 20 < 141,
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const root = page.locator(".detail-body");
  const list = page.locator('[data-window-list="会话活动"]');
  await expect
    .poll(
      async () => {
        await root.evaluate((e) => {
          e.scrollTop = e.scrollHeight;
        });
        return list.getAttribute("data-window-count");
      },
      { timeout: 15000 },
    )
    .toBe("141");
  await root.evaluate((e) => {
    e.scrollTop = 0;
  });
  const bar = page.getByRole("button", { name: /^工具活动/ }).first();
  await expect(bar).toHaveAttribute("aria-expanded", "false");
  await bar.click();
  await expect(page.getByText("工具记录 0", { exact: true })).toBeVisible();
  await expect
    .poll(async () => {
      await root.evaluate(async (e) => {
        e.scrollTop = e.scrollHeight;
        // ResizeObserver and the next React range update must settle before asserting visibility.
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        );
      });
      // Measured tool rows can grow after scrolling; reach the final message as well.
      return (
        (await page.getByText("工具记录 139", { exact: true }).isVisible()) &&
        (await page.getByText("本轮全部完成", { exact: true }).isVisible())
      );
    })
    .toBe(true);
  await expect(page.getByText("本轮全部完成", { exact: true })).toBeVisible();
  expect(
    await list.locator(":scope > [data-window-row]").count(),
  ).toBeLessThanOrEqual(60);
  expect(errors).toEqual([]);
});

test("mobile detail header contracts to one line without losing reading position", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const title = "修复会话正文分页、工具输出分组与手机详情阅读布局";
  await page.route("**/api/flows/42", (route) =>
    route.fulfill({
      json: {
        ...flow,
        title,
        reason: "正在验证",
        next_condition: "等待验证完成",
        sources: [],
      },
    }),
  );
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items: Array.from({ length: 20 }, (_, i) => ({
          ...activity,
          key: `mobile-${i}`,
          previous_version: null,
          body: `第 ${i} 条会话内容`,
          body_length: `第 ${i} 条会话内容`.length,
        })),
        snapshot: 2,
        total: 20,
        has_more: false,
        next_cursor: null,
        sources: [],
        changes: {},
      },
    }),
  );
  await page.goto("/#flow/42/conversation");
  const head = page.locator(".detail-head");
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-fixes-mobile-expanded.png",
  });
  const expanded = await head.evaluate((e) => e.getBoundingClientRect().height);
  const anchorTop = await page.locator(".detail-body").evaluate((e, offset) => {
    e.scrollTop = offset;
    return e.querySelector('[data-entry="mobile-5"]')!.getBoundingClientRect()
      .top;
  }, expanded + 180);
  await expect(head).toHaveClass(/compact/);
  expect(
    Math.abs(
      (await page
        .locator('[data-entry="mobile-5"]')
        .evaluate((e) => e.getBoundingClientRect().top)) - anchorTop,
    ),
  ).toBeLessThan(2);
  expect(
    await head.evaluate((e) => e.getBoundingClientRect().height),
  ).toBeLessThan(65);
  expect(expanded).toBeGreaterThan(150);
  await expect(head.getByRole("tablist")).not.toBeVisible();
  await expect(head.getByRole("button", { name: "返回看板" })).toBeVisible();
  await page.screenshot({
    path: "../../../.amp/in/artifacts/console-fixes-mobile-compact.png",
  });
  await head.getByRole("button", { name: "展开详情标题" }).click();
  await expect(head.getByRole("tablist")).toBeVisible();
  expect(
    await page.locator(".detail-body").evaluate((e) => e.scrollTop),
  ).toBeGreaterThan(350);
  await page.locator(".detail-body").evaluate((e) => {
    e.scrollTop = 0;
  });
  await expect(head).not.toHaveClass(/compact/);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.locator(".detail-body").evaluate((e) => {
    e.scrollTop = 420;
  });
  await expect(head).not.toHaveClass(/compact/);
});
