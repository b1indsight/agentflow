import { expect, test, type Locator } from "@playwright/test";
import type { FlowDetail, JobSummary, SourceStatus } from "../types";

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
  archived_at: null,
  state_version: 2,
};
const blockedFlow: FlowDetail = {
  ...flow,
  lifecycle: "cancelled",
  phase: "blocked",
  action: "cancelled",
  reason: "Flow 已取消",
  next_condition: "修复受阻原因后重试",
  sources: [],
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

test("execution timeline keeps timestamps and metadata when loading older jobs", async ({
  page,
}) => {
  // Retried executions can share kind/attempt; their creation times distinguish them.
  const jobs: JobSummary[] = ["running", "failed", "done", "queued"].map(
    (state, i) => ({
      id: 4 - i,
      flow_id: flow.id,
      attempt: 1,
      kind: "initial",
      state,
      worker_id: state === "queued" ? null : "worker-1",
      created_at: new Date(Date.parse(now) - i * 3_600_000).toISOString(),
      started_at: state === "queued" ? null : now,
      updated_at: now,
      finished_at: state === "failed" || state === "done" ? now : null,
      error_code: state === "failed" ? "execution_error" : null,
    }),
  );
  await page.route("**/api/flows/42/jobs?**", (route) => {
    const older = new URL(route.request().url()).searchParams.get("cursor");
    return route.fulfill({
      json: {
        items: older ? jobs.slice(2) : jobs.slice(0, 2),
        next_cursor: older ? null : "older-jobs",
        has_more: !older,
      },
    });
  });
  await page.goto("/#flow/42/jobs");
  const records = page.getByRole("article");
  await expect(records).toHaveCount(2);
  await expect(records.nth(0)).toContainText("running");
  await expect(records.nth(1)).toContainText("worker-1 · execution_error");
  await page.getByRole("button", { name: "读取更多执行记录" }).click();
  await expect(records).toHaveCount(4);
  await expect(records.locator(".badge")).toHaveText([
    "running",
    "failed",
    "done",
    "queued",
  ]);
  for (const [i, job] of jobs.entries()) {
    await expect(records.nth(i).locator("time")).toHaveAttribute(
      "datetime",
      job.created_at,
    );
    await expect(records.nth(i).locator("time")).toHaveText(
      await page.evaluate(
        (date) => `创建于 ${new Date(date).toLocaleString()}`,
        job.created_at,
      ),
    );
  }
  await expect(records.nth(3)).toContainText("无 Worker");
  await expect(
    page.getByRole("button", { name: "读取更多执行记录" }),
  ).toHaveCount(0);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/jobs-timeline-desktop.png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(records.nth(3)).toBeVisible();
  expect(
    await page
      .locator(".detail-body")
      .evaluate((e) => e.scrollWidth <= e.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: "../../../.amp/in/artifacts/jobs-timeline-mobile.png",
  });
});

test("shared virtual lists preserve distant focus, omitted gaps and keyboard order", async ({
  page,
}) => {
  // A focused first row must stay at its old location while the viewport displays a distant range.
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/flows?**", (route) => {
    const url = new URL(route.request().url());
    const phase = url.searchParams.get("phase");
    const start = Number(url.searchParams.get("cursor") ?? 0);
    const total = phase === "queued" || phase === "running" ? 70 : 0;
    return route.fulfill({
      json: {
        items: Array.from({ length: Math.min(50, total - start) }, (_, i) => ({
          ...flow,
          id: (phase === "running" ? 42 : 200) + start + i,
          phase,
          title: `${phase} Flow ${start + i}`,
        })),
        next_cursor: start + 50 < total ? String(start + 50) : null,
        has_more: start + 50 < total,
        counts: { queued: 70, running: 70, review: 0, blocked: 0, done: 0 },
      },
    });
  });
  await page.route("**/api/flows/111", (route) =>
    route.fulfill({
      json: {
        ...flow,
        id: 111,
        title: "running Flow 69",
        reason: "正在验证",
        next_condition: "等待验证完成",
        sources: [],
      },
    }),
  );
  await page.route("**/api/flows/111/jobs?**", (route) => {
    const start = Number(
      new URL(route.request().url()).searchParams.get("cursor") ?? 0,
    );
    return route.fulfill({
      json: {
        items: Array.from({ length: Math.min(50, 90 - start) }, (_, i) => ({
          id: 90 - start - i,
          flow_id: 111,
          attempt: start + i + 1,
          kind: "initial",
          state: "done",
          worker_id: "worker-1",
          created_at: new Date(
            Date.parse(now) - (start + i) * 1000,
          ).toISOString(),
          started_at: now,
          updated_at: now,
          finished_at: now,
          error_code: null,
        })),
        next_cursor: start + 50 < 90 ? String(start + 50) : null,
        has_more: start + 50 < 90,
      },
    });
  });
  const checkPinnedNavigation = async (
    root: Locator,
    first: Locator,
    next: Locator,
    last: Locator,
  ) => {
    await root.evaluate((node) => {
      node.scrollTop = 0;
    });
    await first.focus();
    await root.evaluate(async (node) => {
      node.scrollTop = node.scrollHeight;
      for (let i = 0; i < 4; i++) await new Promise(requestAnimationFrame);
    });
    await expect(last).toBeInViewport();
    await expect(first).toBeFocused();
    await expect(first).not.toBeInViewport();
    expect(await root.evaluate((node) => node.scrollTop)).toBeGreaterThan(2000);
    await page.keyboard.press("Tab");
    await expect(next).toBeFocused();
    await expect(next).toBeInViewport();
    await page.keyboard.press("Shift+Tab");
    await expect(first).toBeFocused();
    await expect(first).toBeInViewport();
  };
  await page.goto("/#board");
  for (const phase of ["queued", "running"]) {
    const root = page.locator(`[data-phase="${phase}"]`);
    await expect
      .poll(async () => {
        await root.evaluate((node) => {
          node.scrollTop = node.scrollHeight;
        });
        return root
          .locator("[data-window-list]")
          .getAttribute("data-window-count");
      })
      .toBe("70");
  }
  const board = page.locator('[data-phase="running"]');
  await checkPinnedNavigation(
    board,
    board.getByRole("button", { name: /running Flow 0 / }),
    board.getByRole("button", { name: /running Flow 1 / }),
    board.getByRole("button", { name: /running Flow 69 / }),
  );
  await board.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await board.getByRole("button", { name: /running Flow 69 / }).click();
  const rail = page.locator(".rail-scroll");
  await expect(
    rail.locator('[data-window-list="侧栏-queued"]'),
  ).toHaveAttribute("data-window-count", "70");
  await expect(
    rail.locator('[data-window-list="侧栏-running"]'),
  ).toHaveAttribute("data-window-count", "70");
  await checkPinnedNavigation(
    rail,
    rail.getByRole("button", { name: "queued Flow 0", exact: true }),
    rail.getByRole("button", { name: "queued Flow 1", exact: true }),
    rail.getByRole("button", { name: "running Flow 69", exact: true }),
  );
  await page.getByRole("tab", { name: "执行记录", exact: true }).click();
  await page.getByRole("button", { name: "读取更多执行记录" }).click();
  const jobs = page.locator('[data-window-list="执行记录"]');
  await expect(jobs).toHaveAttribute("data-window-count", "90");
  const detail = page.locator(".detail-body");
  await detail.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expect(page.getByText("initial #90", { exact: true })).toBeInViewport();
  await detail.evaluate((node) => {
    node.scrollTop = 0;
  });
  await expect(page.getByText("initial #1", { exact: true })).toBeInViewport();
  for (const list of await page.locator("[data-window-list]").all())
    expect(
      await list.locator(":scope > [data-window-row]").count(),
    ).toBeLessThanOrEqual(60);
  expect(errors).toEqual([]);
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

test("retry disables both commands until commit, then refreshes detail and board", async ({
  page,
}) => {
  let current = blockedFlow;
  await page.route("**/api/flows?**", (route) =>
    route.fulfill({
      json: {
        items:
          new URL(route.request().url()).searchParams.get("phase") ===
          current.phase
            ? [current]
            : [],
        counts: {
          queued: current.phase === "queued" ? 1 : 0,
          running: 0,
          review: 0,
          blocked: current.phase === "blocked" ? 1 : 0,
          done: 0,
        },
        next_cursor: null,
        has_more: false,
      },
    }),
  );
  await page.route("**/api/flows/42", (route) =>
    route.fulfill({ json: current }),
  );
  let finish = () => {};
  const committed = new Promise<void>((resolve) => {
    finish = resolve;
  });
  await page.route("**/api/flows/42/retry", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["content-type"]).toBe("application/json");
    expect(route.request().postDataJSON()).toEqual({});
    await committed;
    current = {
      ...current,
      lifecycle: "active",
      phase: "queued",
      action: "initial:queued",
      state_version: 3,
      reason: "等待 Worker",
      next_condition: "等待调度",
    };
    return route.fulfill({ json: current });
  });
  await page.goto("/#flow/42/overview");
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "正在提交重试…" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "归档", exact: true }),
  ).toBeDisabled();
  await expect(page.locator(".rail-group.phase-blocked .count")).toHaveText(
    "1",
  );
  finish();
  await expect(page.getByRole("status")).toContainText("重试请求已受理");
  await expect(page.locator(".detail-head .badge")).toHaveText(
    "initial:queued",
  );
  await expect(page.locator(".rail-group.phase-blocked .count")).toHaveText(
    "0",
  );
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(page.getByText("真实活动正文", { exact: true })).toBeVisible();
  await page.evaluate(() => {
    location.hash = "board";
  });
  await expect(page.locator(".column.phase-queued .card")).toContainText(
    flow.title,
  );
  await expect(page.locator(".column.phase-blocked .card")).toHaveCount(0);
});

for (const [code, message] of [
  ["retry_stop_pending", "停止确认"],
  ["retry_no_change", "没有可重试的任务"],
  ["network", "无法确认重试是否已提交"],
]) {
  test(`retry failure explains ${code} without reporting success or resending`, async ({
    page,
  }) => {
    await page.route("**/api/flows/42", (route) =>
      route.fulfill({ json: blockedFlow }),
    );
    let requests = 0;
    await page.route("**/api/flows/42/retry", (route) => {
      requests++;
      return code === "network"
        ? route.abort("failed")
        : route.fulfill({ status: 409, json: { detail: code } });
    });
    await page.goto("/#flow/42/overview");
    const retry = page.getByRole("button", { name: "重试", exact: true });
    await retry.click();
    await expect(page.getByRole("alert")).toContainText(message);
    await expect(page.getByRole("status")).toHaveCount(0);
    await expect(retry).toBeEnabled();
    expect(requests).toBe(1);
  });
}

for (const width of [1440, 390]) {
  test(`cancelled flow manual archive persists and refreshes groups at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    let current: FlowDetail = {
      ...flow,
      lifecycle: "cancelled",
      phase: "blocked",
      action: "cancelled",
      title: "已取消的 Flow",
      worker_historical: true,
      reason: "Flow 已取消",
      next_condition: "确认在途任务的停止确认与结果对账完成后，再评估是否重试",
      sources: [],
    };
    const detail = () => ({ ...current });
    await page.route("**/api/flows?**", (route) =>
      route.fulfill({
        json: {
          items:
            new URL(route.request().url()).searchParams.get("phase") ===
            current.phase
              ? [current]
              : [],
          counts: {
            queued: 0,
            running: 0,
            review: 0,
            blocked: current.archived_at ? 0 : 1,
            done: current.archived_at ? 1 : 0,
          },
          next_cursor: null,
          has_more: false,
        },
      }),
    );
    await page.route("**/api/flows/42", (route) =>
      route.fulfill({ json: detail() }),
    );
    let attempts = 0;
    let finish: () => void = () => {};
    const committed = new Promise<void>((resolve) => {
      finish = resolve;
    });
    await page.route("**/api/flows/42/archive", async (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["content-type"]).toBe(
        "application/json",
      );
      expect(route.request().postDataJSON()).toEqual({});
      if (++attempts === 1)
        return route.fulfill({
          status: 503,
          json: { detail: "database_unavailable" },
        });
      await committed;
      current = {
        ...current,
        phase: "done",
        archived_at: now,
        state_version: 3,
        reason: "Flow 已取消并归档；归档仅改变展示分组",
      };
      return route.fulfill({ json: detail() });
    });
    await page.goto("/?q=repo%3Aowner%2Fagentflow#flow/42/overview");
    const archive = page.getByRole("button", { name: "归档", exact: true });
    await expect(archive).toBeVisible();
    await expect(
      page.getByRole("button", { name: "重试", exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: `../../../.amp/in/artifacts/cancelled-archive-before-${width}.png`,
    });
    await archive.click();
    await expect(page.getByRole("alert")).toContainText("归档失败");
    await expect(archive).toBeEnabled();
    await expect(page.locator(".detail-head .badge")).toHaveText("已取消");
    if (width === 1440)
      await page.screenshot({
        path: "../../../.amp/in/artifacts/cancelled-archive-error.png",
      });
    await archive.click();
    await expect(
      page.getByRole("button", { name: "正在归档…" }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "重试", exact: true }),
    ).toBeDisabled();
    finish();
    await expect(page.locator(".detail-head .badge")).toHaveText(
      "已取消 · 已归档",
    );
    await expect(page.getByRole("alert")).toHaveCount(0);
    await expect(archive).toHaveCount(0);
    await expect(
      page.getByText("已取消 / 已归档", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("手动归档", { exact: true })).toBeVisible();
    if (width === 1440) {
      await expect(page.locator(".rail-group.phase-blocked .count")).toHaveText(
        "0",
      );
      await expect(page.locator(".rail-group.phase-done .count")).toHaveText(
        "1",
      );
      await expect(page.locator(".rail-group.phase-done .flow-tab")).toHaveText(
        current.title!,
      );
    }
    await page.screenshot({
      path: `../../../.amp/in/artifacts/cancelled-archive-after-${width}.png`,
    });
    await page.reload();
    await expect(page.locator(".detail-head .badge")).toHaveText(
      "已取消 · 已归档",
    );
    await expect(page).toHaveURL(
      /q=repo%3Aowner%2Fagentflow#flow\/42\/overview/,
    );
    await page.getByRole("tab", { name: "会话" }).click();
    await expect(page.getByText("真实活动正文", { exact: true })).toBeVisible();
    await page.evaluate(() => {
      location.hash = "board";
    });
    if (width === 390)
      await page
        .locator(".mobile-stages")
        .getByRole("button", { name: /已归档/ })
        .click();
    await expect(page.locator(".column.phase-done .card")).toContainText(
      "已取消 · 已归档",
    );
    await expect(page.locator(".column.phase-blocked .card")).toHaveCount(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  });
}

test("a retry finishing after navigation cannot overwrite another flow", async ({
  page,
}) => {
  await page.route("**/api/flows/42", (route) =>
    route.fulfill({ json: blockedFlow }),
  );
  await page.route("**/api/flows/43", (route) =>
    route.fulfill({
      json: {
        ...flow,
        id: 43,
        title: "另一条 Flow",
        reason: "正在执行",
        next_condition: "等待结果",
        sources: [],
      },
    }),
  );
  let finish = () => {};
  const committed = new Promise<void>((resolve) => {
    finish = resolve;
  });
  await page.route("**/api/flows/42/retry", async (route) => {
    await committed;
    return route.fulfill({
      json: {
        ...flow,
        phase: "queued",
        state_version: 3,
        reason: "已重排",
        next_condition: "等待 Worker",
        sources: [],
      },
    });
  });
  await page.goto("/#flow/42/overview");
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "正在提交重试…" }),
  ).toBeDisabled();
  await page.evaluate(() => {
    location.hash = "flow/43/overview";
  });
  await expect(
    page.getByRole("heading", { name: "另一条 Flow", exact: true }),
  ).toBeVisible();
  const completed = page.waitForResponse("**/api/flows/42/retry");
  finish();
  await completed;
  await expect(
    page.getByRole("heading", { name: "另一条 Flow", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "重试", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("overview sources explain sync progress separately from historical gaps", async ({
  page,
}) => {
  // A new sync retains its last result: pending + no_session must not claim
  // there is still no session, and a history gap must not look like a read error.
  const sources: SourceStatus[] = [
    {
      source: "github",
      status: "pending",
      reason: null,
      last_success_at: "2026-10-20T06:42:31.689912Z",
      limitations: ["remote_deleted_or_pre_ingestion_versions_unrecoverable"],
    },
    {
      source: "issue.comments",
      status: "available",
      reason: null,
      last_success_at: "2026-10-20T06:42:33.701871Z",
      limitations: [],
    },
    {
      source: "issue.description",
      status: "partial",
      reason: null,
      last_success_at: "2026-10-20T06:42:32.510914Z",
      limitations: ["pre_ingestion_edits_and_deleted_bodies_unrecoverable"],
    },
    {
      source: "issue.reactions",
      status: "partial",
      reason: null,
      last_success_at: "2026-10-20T06:42:32.557021Z",
      limitations: ["reaction_removals_have_no_webhook_or_revision_clock"],
    },
    {
      source: "issue.timeline",
      status: "available",
      reason: null,
      last_success_at: "2026-10-20T06:42:32.505066Z",
      limitations: [],
    },
    {
      source: "worker",
      status: "pending",
      reason: "no_session",
      last_success_at: null,
      limitations: [],
    },
  ];
  await page.route("**/api/flows/42", (route) =>
    route.fulfill({
      json: {
        ...flow,
        reason: "正在验证",
        next_condition: "等待验证结果",
        sources,
      },
    }),
  );
  await page.goto("/#flow/42/overview");
  const list = page.getByRole("list", { name: "数据来源明细" });
  const github = list.getByRole("listitem").filter({
    has: page.getByText("GitHub 同步入口", { exact: true }),
  });
  await expect(github).toContainText("等待同步");
  await expect(github).toContainText("上次成功同步：");
  await expect(github.locator("time")).toHaveAttribute(
    "datetime",
    "2026-10-20T06:42:31.689912Z",
  );
  await expect(list.getByText("历史记录受限", { exact: true })).toHaveCount(2);
  await expect(list).toContainText("无法完整追踪表情回应的移除记录。");
  await expect(list).toContainText("上次读取：尚无执行会话");
  await expect(list).toContainText("上次成功同步：暂无记录");
  await expect(list).not.toContainText("no_session");
  await expect(list).not.toContainText("unrecoverable");
  await expect(list.getByText("读取失败", { exact: true })).toHaveCount(0);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1200 });
    await list.scrollIntoViewIfNeeded();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await list.screenshot({
      path: `../../../.amp/in/artifacts/source-list-${width}.png`,
    });
  }
});

test("conversation sources keep routine diagnostics collapsed and surface actionable failures", async ({
  page,
}) => {
  // Historical coverage limits and a not-yet-created worker session are normal
  // on a new flow; an actual read failure must still be visible and retryable.
  const historyLimit = "pre_ingestion_edits_and_deleted_bodies_unrecoverable";
  const historyExplanation = "无法恢复首次采集前的正文编辑历史和已删除的正文。";
  let sources: SourceStatus[] = [
    {
      source: "issue.description",
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
  await expect(
    status.getByText(historyExplanation, { exact: true }),
  ).toBeHidden();
  await expect(warning).toHaveCount(0);
  await details.locator("summary").click();
  await expect(
    status.getByText("GitHub 最多返回此 PR 的 250 条提交记录。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    status.getByText("评审讨论状态缺少远端变更时间，无法精确还原变更历史。", {
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
  await expect(warning).toContainText("Issue 时间线：来源返回了无法识别的数据");
  await expect(warning).not.toContainText("PR 提交记录");
  await expect(warning).not.toContainText("PR 评审讨论");
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
  await expect(
    status.getByText(historyExplanation, { exact: true }),
  ).toBeVisible();
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
  const github = message.getByRole("link", { name: "在 GitHub 中查看" });
  await expect(github).toHaveAttribute("href", description.url);
  await expect(github).toHaveAttribute("title", "在 GitHub 中查看");
  await expect(github).toHaveText("");
  await expect(github.locator("svg")).toBeVisible();
  const metadata = message.locator(".message-provenance");
  const toggle = message.getByRole("button", { name: "来源信息与查询定位" });
  await expect(toggle).toHaveAccessibleName("来源信息与查询定位");
  await expect(toggle).toHaveAttribute("title", "来源信息与查询定位");
  await expect(toggle).toHaveText("");
  await expect(toggle.locator("svg")).toBeVisible();
  const expectUnframedIcon = async () => {
    for (const control of [github, toggle]) {
      await expect(control).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(control).toHaveCSS("border-top-width", "0px");
      await expect(control).toHaveCSS("outline-style", "none");
    }
  };
  const expectCornerIcon = async () => {
    // Both controls must stay adjacent when the separate metadata panel expands.
    const icon = await toggle.boundingBox();
    const footer = await message.locator(".message-footer").boundingBox();
    const link = await github.boundingBox();
    if (!icon || !footer || !link) throw new Error("Message footer is missing");
    expect(icon.width).toBeLessThanOrEqual(28);
    expect(icon.height).toBeGreaterThanOrEqual(24);
    expect(link.width).toBeLessThanOrEqual(28);
    expect(
      Math.abs(icon.x + icon.width - footer.x - footer.width),
    ).toBeLessThan(1);
    expect(Math.abs(link.y - icon.y)).toBeLessThan(1);
    expect(icon.x - link.x - link.width).toBeCloseTo(4);
    // Equal SVG boxes can still contain differently sized drawings and padding.
    const [githubInk, infoInk] = await message
      .locator(".message-actions")
      .evaluate((actions) =>
        [...actions.querySelectorAll("svg")].map((svg) => {
          const shape = svg.querySelector("circle, path");
          if (!shape) throw new Error("Icon drawing is missing");
          const bounds = shape.getBoundingClientRect();
          const style = getComputedStyle(shape);
          const stroke =
            style.stroke === "none"
              ? 0
              : (parseFloat(style.strokeWidth) *
                  svg.getBoundingClientRect().width) /
                svg.viewBox.baseVal.width;
          return {
            width: bounds.width + stroke,
            height: bounds.height + stroke,
            centerY: bounds.y + bounds.height / 2,
          };
        }),
      );
    expect(Math.abs(githubInk.width - infoInk.width)).toBeLessThan(0.5);
    expect(Math.abs(githubInk.height - infoInk.height)).toBeLessThan(0.5);
    expect(Math.abs(githubInk.centerY - infoInk.centerY)).toBeLessThan(0.5);
  };
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(metadata).not.toBeVisible();
  await expectCornerIcon();
  await expectUnframedIcon();
  await page.locator(".content").screenshot({
    path: "../../../.amp/in/artifacts/activity-icons-aligned-desktop.png",
  });
  await toggle.hover();
  await expectUnframedIcon();
  await page.locator(".content").screenshot({
    path: "../../../.amp/in/artifacts/activity-icons-aligned-hover-desktop.png",
  });
  await github.focus();
  await github.press("Tab");
  await expect(toggle).toBeFocused();
  await toggle.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(metadata.getByLabel("文本")).toContainText("issue_number");
  await expectCornerIcon();
  await expectUnframedIcon();
  await expect(toggle.locator("svg path").first()).toHaveCSS(
    "stroke-width",
    "3px",
  );
  await page.locator(".content").screenshot({
    path: "../../../.amp/in/artifacts/activity-icons-aligned-details-desktop.png",
  });
  await toggle.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(metadata).not.toBeVisible();
  await toggle.evaluate((element) => element.blur());
  await page.setViewportSize({ width: 390, height: 844 });
  await expectCornerIcon();
  await expectUnframedIcon();
  await page.locator(".content").screenshot({
    path: "../../../.amp/in/artifacts/activity-icons-aligned-mobile.png",
  });
  await toggle.click();
  await expect(metadata.getByLabel("文本")).toContainText("issue_number");
  await expectCornerIcon();
  await expectUnframedIcon();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.locator(".content").screenshot({
    path: "../../../.amp/in/artifacts/activity-icons-aligned-details-mobile.png",
  });
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(metadata.getByLabel("文本")).toContainText("issue_number");
  await expectCornerIcon();
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
  let refreshed = false,
    rebuilding = false;
  let releaseRefresh!: () => void, releasePage!: () => void;
  const refreshGate = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  const pageGate = new Promise<void>((resolve) => {
    releasePage = resolve;
  });
  const rereads: string[] = [],
    errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/flows/42/activity?**", async (route) => {
    const url = new URL(route.request().url());
    const snapshot =
      url.searchParams.get("refresh") === "true"
        ? 3
        : Number(url.searchParams.get("snapshot") ?? 2);
    const start = Number(url.searchParams.get("cursor") ?? 0);
    if (url.searchParams.get("refresh") === "true") {
      refreshed = true;
      await refreshGate;
    }
    if (snapshot === 3 && start === 100) {
      rebuilding = true;
      await pageGate;
    }
    rereads.push(`${start}:${url.searchParams.get("snapshot")}`);
    const items = Array.from({ length: Math.min(20, 320 - start) }, (_, i) => ({
      ...activity,
      key: `large-${start + i}`,
      // Refresh uses the archive's ascending (occurred_at, key) order to detect removed targets.
      occurred_at: new Date(Date.parse(now) + (start + i) * 1000).toISOString(),
      version: snapshot,
      body: `归档正文 ${start + i}`,
      body_length: `归档正文 ${start + i}`.length,
    }));
    return route.fulfill({
      json: {
        items,
        snapshot,
        total: 320,
        next_cursor: start + 20 < 320 ? String(start + 20) : null,
        has_more: start + 20 < 320,
        sources: [],
        changes: snapshot === 3 ? { edited: 1 } : {},
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
  await expect.poll(() => refreshed).toBe(true);
  // Move after both the first response and a later pinned page begin waiting. Neither may restore old pixels.
  await root.evaluate(async (node) => {
    node.scrollTop = node.scrollHeight;
    for (let i = 0; i < 6; i++) await new Promise(requestAnimationFrame);
  });
  releaseRefresh();
  await expect.poll(() => rebuilding).toBe(true);
  const bounds = (await root.boundingBox())!;
  await page.mouse.move(
    bounds.x + bounds.width / 2,
    bounds.y + bounds.height / 2,
  );
  await page.mouse.wheel(0, -3500);
  const latest = await root.evaluate(async (node) => {
    for (let i = 0; i < 6; i++) await new Promise(requestAnimationFrame);
    const top = node.getBoundingClientRect().top + node.clientTop;
    const entry = [
      ...node.querySelectorAll<HTMLElement>("article[data-entry]"),
    ].find((item) => {
      const rect = item.parentElement!.getBoundingClientRect();
      return rect.top >= top && rect.bottom <= top + node.clientHeight;
    })!;
    return {
      key: entry.dataset.entry!,
      offset: entry.getBoundingClientRect().top - top,
    };
  });
  expect(Number(latest.key.slice("large-".length))).toBeGreaterThan(100);
  expect(Number(latest.key.slice("large-".length))).toBeLessThan(300);
  releasePage();
  await expect(
    page.getByRole("status").filter({ hasText: "快照更新" }),
  ).toBeVisible();
  const positions = await root.evaluate(async (node, latest) => {
    const values: number[] = [];
    for (let i = 0; i < 18; i++) {
      await new Promise<void>((resolve) =>
        requestAnimationFrame(() => setTimeout(resolve, 0)),
      );
      const entry = node.querySelector(`[data-entry="${latest.key}"]`);
      values.push(
        entry
          ? entry.getBoundingClientRect().top -
              node.getBoundingClientRect().top -
              node.clientTop
          : Infinity,
      );
    }
    return values;
  }, latest);
  expect(
    Math.max(...positions.map((offset) => Math.abs(offset - latest.offset))),
  ).toBeLessThan(2);
  expect(errors).toEqual([]);
});

for (const scenario of [
  "messages",
  "collapsed tool groups",
  "expanded tool groups",
]) {
  test(`scrolling up from the conversation boundary keeps ${scenario} steady`, async ({
    page,
  }) => {
    test.setTimeout(45000);
    // Moving the mounted boundary must not change author headers or move results between calls and rows.
    const tools = scenario !== "messages";
    const expanded = scenario === "expanded tool groups";
    const total = tools ? 180 : 320;
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") errors.push(message.text());
    });
    await page.addInitScript(() => {
      window.addEventListener("error", (event) => console.error(event.message));
    });
    await page.route("**/api/flows/42/activity?**", (route) => {
      const start = Number(
        new URL(route.request().url()).searchParams.get("cursor") ?? 0,
      );
      return route.fulfill({
        json: {
          items: Array.from({ length: 20 }, (_, i) => {
            const n = start + i;
            const message = expanded ? n === 179 : n % 20 === 19;
            const body =
              n % 5 === 0
                ? `第 ${n} 条消息\n\n\`\`\`text\n${"检查变高会话的阅读位置\n".repeat(3 + (n % 11))}\`\`\``
                : `第 ${n} 条消息。${"向上滚动时保留正文阅读位置。".repeat(1 + (n % 4))}`;
            return {
              ...activity,
              key: `steady-${n}`,
              source: tools ? "worker" : activity.source,
              kind: tools
                ? message
                  ? "message"
                  : n % 2
                    ? "tool_result"
                    : "tool_call"
                : activity.kind,
              title: tools ? "exec" : activity.title,
              author: tools && !message && n % 2 ? "tool" : "assistant",
              body,
              body_length: body.length,
              previous_version: null,
              metadata: tools
                ? {
                    attempt_id: 1,
                    session_id: "steady-tools",
                    session_generation: 0,
                    call_id: `call-${Math.floor(n / 2)}`,
                  }
                : {},
            };
          }),
          snapshot: 2,
          total,
          next_cursor: start + 20 < total ? String(start + 20) : null,
          has_more: start + 20 < total,
          sources: [],
          changes: {},
        },
      });
    });
    await page.goto("/#flow/42/conversation");
    const root = page.locator(".detail-body");
    const more = page.getByRole("button", {
      name: "读取更多交互",
      exact: true,
    });
    await expect
      .poll(
        async () => {
          await root.evaluate((element) => {
            element.scrollTop = element.scrollHeight;
          });
          // This setup deliberately reads the complete folded run before expanding it.
          if (expanded && (await more.isVisible())) await more.click();
          return page
            .getByRole("button", {
              name: "已到当前本地快照末尾 · 点击刷新",
              exact: true,
            })
            .isVisible();
        },
        { timeout: 15000 },
      )
      .toBe(true);
    if (expanded)
      await page
        .getByRole("button", { name: /^工具活动/ })
        .first()
        .click();
    await expect
      .poll(
        () =>
          root.evaluate(async (element, total) => {
            element.scrollTop = element.scrollHeight;
            for (let i = 0; i < 4; i++)
              await new Promise(requestAnimationFrame);
            const bounds = element
              .querySelector(`[data-entry="steady-${total - 1}"]`)
              ?.getBoundingClientRect();
            const viewport = element.getBoundingClientRect();
            return Boolean(
              bounds &&
              bounds.top < viewport.bottom &&
              bounds.bottom > viewport.top,
            );
          }, total),
        { timeout: 15000, intervals: [100] },
      )
      .toBe(true);
    const bounds = (await root.boundingBox())!;
    await page.mouse.move(
      bounds.x + bounds.width / 2,
      bounds.y + bounds.height / 2,
    );
    for (let step = 0; step < 18; step++) {
      // Sample during input too: a new body can outgrow its old position before the range settles.
      const [frames] = await Promise.all([
        root.evaluate(async (element) => {
          let overlap = 0,
            rows = 0,
            bodies = 0,
            emptyFrames = 0;
          for (let frame = 0; frame < 18; frame++) {
            await new Promise(requestAnimationFrame);
            const viewport = element.getBoundingClientRect();
            const mounted = [
              ...element.querySelectorAll(
                ".conversation-stream [data-window-row]",
              ),
            ];
            const rectangles = mounted
              .map((row) => row.getBoundingClientRect())
              .filter((rectangle) => rectangle.height > 0);
            // An empty window must not pass merely because there are no overlapping rows.
            if (
              !rectangles.some(
                (row) => row.top < viewport.bottom && row.bottom > viewport.top,
              )
            )
              emptyFrames++;
            for (let i = 1; i < rectangles.length; i++) {
              const previous = rectangles[i - 1],
                current = rectangles[i];
              if (
                current.top < viewport.bottom &&
                previous.bottom > viewport.top
              )
                overlap = Math.max(overlap, previous.bottom - current.top);
            }
            rows = Math.max(rows, mounted.length);
            bodies = Math.max(
              bodies,
              element.querySelectorAll(
                '.message-body, pre[aria-label="工具摘要"]',
              ).length,
            );
          }
          return { overlap, rows, bodies, emptyFrames };
        }),
        page.mouse.wheel(0, -240),
      ]);
      expect(
        frames.overlap,
        `Rows overlapped during wheel step ${step}`,
      ).toBeLessThan(1);
      expect(frames.rows).toBeLessThanOrEqual(60);
      expect(frames.bodies).toBeLessThanOrEqual(60);
      expect(frames.emptyFrames, `Empty window during wheel step ${step}`).toBe(
        0,
      );
      const movement = await root.evaluate(async (element) => {
        // Check several painted frames after input, not just the eventual final position.
        for (let i = 0; i < 6; i++) await new Promise(requestAnimationFrame);
        const top = element.getBoundingClientRect().top;
        const anchor = [...element.querySelectorAll("[data-entry]")].find(
          (row) => row.getBoundingClientRect().bottom > top + 100,
        )!;
        const positions: number[] = [];
        for (let i = 0; i < 12; i++) {
          await new Promise(requestAnimationFrame);
          positions.push(anchor.getBoundingClientRect().top);
        }
        return Math.max(...positions) - Math.min(...positions);
      });
      expect(
        movement,
        `Reading anchor oscillated after wheel step ${step}`,
      ).toBeLessThan(2);
    }
    expect(
      await page.locator(".conversation-stream [data-window-row]").count(),
    ).toBeLessThanOrEqual(60);
    expect(errors).toEqual([]);
  });
}

test("height changes above, within and below the reading row preserve its top through every frame", async ({
  page,
}) => {
  // Unlike wheel-only checks, asymmetric expansion distinguishes anchoring from shifting by every height delta.
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/flows/42/activity?**", (route) => {
    const start = Number(
      new URL(route.request().url()).searchParams.get("cursor") ?? 0,
    );
    return route.fulfill({
      json: {
        items: Array.from({ length: Math.min(20, 100 - start) }, (_, i) => {
          const n = start + i;
          const body = `高度校准正文 ${n}。${"保留稳定阅读位置。".repeat(3)}`;
          return {
            ...activity,
            key: `resize-${n}`,
            author: n % 2 ? "reviewer" : "owner",
            body,
            body_length: body.length,
            previous_version: null,
            metadata: {
              diagnostics: Array.from(
                { length: 12 },
                (_, i) => `${n}-${i} ${"完整来源信息。".repeat(4)}`,
              ),
            },
          };
        }),
        snapshot: 2,
        total: 100,
        next_cursor: start + 20 < 100 ? String(start + 20) : null,
        has_more: start + 20 < 100,
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const root = page.locator(".detail-body");
  await expect
    .poll(async () => {
      await root.evaluate((node) => {
        node.scrollTop = node.scrollHeight;
      });
      return page
        .getByRole("button", {
          name: "已到当前本地快照末尾 · 点击刷新",
          exact: true,
        })
        .isVisible();
    })
    .toBe(true);
  const key = await root.evaluate(async (node) => {
    node.scrollTop = node.scrollHeight - 1800;
    for (let i = 0; i < 6; i++) await new Promise(requestAnimationFrame);
    const top = node.getBoundingClientRect().top + node.clientTop;
    const bottom = top + node.clientHeight;
    return [...node.querySelectorAll<HTMLElement>("article[data-entry]")].find(
      (entry) => {
        const rect = entry.parentElement!.getBoundingClientRect();
        return rect.top >= top && rect.bottom <= bottom;
      },
    )!.dataset.entry!;
  });
  const index = Number(key.slice("resize-".length));
  const anchor = page.locator(`article[data-entry="${key}"]`);
  const before = await anchor.evaluate(
    (entry) => entry.getBoundingClientRect().top,
  );
  for (const relative of [-2, 0, 2]) {
    const toggle = page
      .locator(`article[data-entry="resize-${index + relative}"]`)
      .getByRole("button", { name: "来源信息与查询定位", exact: true });
    await toggle.evaluate((button) => button.focus({ preventScroll: true }));
    for (const expanded of ["true", "false"]) {
      const [frames] = await Promise.all([
        anchor.evaluate(async (entry) => {
          const values: number[] = [];
          let overlap = 0;
          const root = entry.closest<HTMLElement>("[data-scroll-root]")!;
          for (let i = 0; i < 18; i++) {
            // rAF precedes ResizeObserver delivery; sample after that paint, without skipping a frame.
            await new Promise<void>((resolve) =>
              requestAnimationFrame(() => setTimeout(resolve, 0)),
            );
            values.push(entry.getBoundingClientRect().top);
            const viewport = root.getBoundingClientRect();
            const rows = [
              ...root.querySelectorAll(
                ".conversation-stream [data-window-row]",
              ),
            ].map((row) => row.getBoundingClientRect());
            for (let index = 1; index < rows.length; index++)
              if (
                rows[index].top < viewport.bottom &&
                rows[index - 1].bottom > viewport.top
              )
                overlap = Math.max(
                  overlap,
                  rows[index - 1].bottom - rows[index].top,
                );
          }
          return { positions: values, overlap };
        }),
        page.keyboard.press("Enter"),
      ]);
      await expect(toggle).toHaveAttribute("aria-expanded", expanded);
      expect(
        frames.overlap,
        `Rows overlapped when row ${relative} changed height`,
      ).toBeLessThan(1);
      expect(
        Math.max(...frames.positions.map((top) => Math.abs(top - before))),
        `Offset changed when row ${relative} ${expanded === "true" ? "grew" : "shrank"}: ${JSON.stringify(frames.positions)}`,
      ).toBeLessThan(2);
    }
  }
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

test("consecutive authors share identity while conversation bodies render safe Markdown", async ({
  page,
}) => {
  // Codex emits multiple developer instructions in one session; page boundaries must not repeat identity.
  const context = {
    attempt_id: 1,
    session_id: "session-a",
    session_generation: 0,
  };
  const message = (key: string, author: string, body: string) => ({
    ...activity,
    key,
    source: "worker",
    kind: "message",
    title: "message",
    author,
    body,
    body_length: Array.from(body).length,
    previous_version: null,
    metadata: context,
  });
  const command =
    "npm run build -- --example=" +
    "long-argument-".repeat(12) +
    "\n  echo " +
    "x".repeat(96) +
    " --complete";
  const items = [
    message(
      "instructions-a",
      "developer",
      "## 会话阅读\n\n- 连续同作者共用 **作者栏**\n- 正文支持 `Markdown`\n\n[查看说明](https://example.test/guide)",
    ),
    message(
      "instructions-b",
      "developer",
      `> 保留每条消息的来源信息。\n\n\`\`\`sh\n${command}\n\`\`\`\n\n| 内容 | 状态 |\n| --- | --- |\n| Markdown | 已启用 |`,
    ),
    message("system", "system", "系统消息保留独立作者栏。"),
    message("instructions-c", "developer", "间隔其他作者后重新显示作者栏。"),
    {
      ...message("other-session", "developer", "不同会话不合并。"),
      metadata: { ...context, session_id: "session-b" },
    },
    {
      ...message("unattributed", "developer", "缺少归属不合并。"),
      metadata: {},
    },
    {
      ...message("unattributed-2", "developer", "归属不明仍独立。"),
      metadata: {},
    },
    {
      ...message("github-a", "owner", "### GitHub 评论\n\n支持 **Markdown**。"),
      source: "pr.comments",
      kind: "comment",
      title: "评论",
      metadata: { comment_id: 1 },
      url: "https://example.test/comments/1",
    },
    {
      ...message("github-b", "owner", "同作者下一条评论。"),
      source: "pr.comments",
      kind: "comment",
      title: "评论",
      metadata: { comment_id: 2 },
      url: "https://example.test/comments/2",
    },
  ];
  await page.route("**/api/flows/42/activity?**", (route) => {
    const later = new URL(route.request().url()).searchParams.has("cursor");
    return route.fulfill({
      json: {
        items: later ? items.slice(1) : items.slice(0, 1),
        snapshot: 2,
        total: items.length,
        has_more: !later,
        next_cursor: later ? null : "instructions",
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const first = page.locator("article[data-entry='instructions-a']");
  const second = page.locator("article[data-entry='instructions-b']");
  await expect(second).toHaveClass(/continuation/);
  await expect(first.getByRole("heading", { name: "会话阅读" })).toBeVisible();
  await expect(first.locator("li")).toHaveCount(2);
  await expect(first.locator("strong")).toHaveText("作者栏");
  await expect(first.locator(".message-body code")).toHaveText("Markdown");
  await expect(first.getByRole("link", { name: "查看说明" })).toHaveAttribute(
    "href",
    "https://example.test/guide",
  );
  await expect(second.locator("blockquote")).toHaveText(
    "保留每条消息的来源信息。",
  );
  await expect(second.locator("pre code")).toContainText("npm run build");
  await expect(second.getByRole("cell", { name: "已启用" })).toBeVisible();
  await expect(second.locator(".message-label")).not.toBeVisible();
  for (const key of [
    "system",
    "instructions-c",
    "other-session",
    "unattributed",
    "unattributed-2",
  ])
    await expect(
      page.locator(`article[data-entry='${key}'] .message-label`),
    ).toBeVisible();
  const github = page.locator("article[data-entry='github-b']");
  await expect(github.locator(".message-label")).not.toBeVisible();
  await expect(
    github.getByRole("link", { name: "在 GitHub 中查看" }),
  ).toHaveAttribute("href", "https://example.test/comments/2");
  const capture = async (name: string) => {
    await first.scrollIntoViewIfNeeded();
    const a = await first.boundingBox();
    const b = await second.boundingBox();
    if (!a || !b) throw new Error("Developer messages are not mounted");
    await page.screenshot({
      path: `../../../.amp/in/artifacts/conversation-markdown-${name}.png`,
      clip: { x: a.x, y: a.y, width: a.width, height: b.y + b.height - a.y },
    });
  };
  await page.setViewportSize({ width: 1440, height: 1200 });
  await capture("desktop");
  await page.setViewportSize({ width: 390, height: 1200 });
  await capture("mobile");
  const code = second.locator(".message-body pre");
  // Narrow screens must show the full command, including unbroken tokens, without horizontal scrolling.
  await expect(code).toHaveText(`${command}\n`);
  await expect
    .poll(() =>
      code.evaluate((element) => element.scrollWidth <= element.clientWidth),
    )
    .toBe(true);
  expect(
    await code.evaluate((element) => {
      const style = getComputedStyle(element);
      return (
        element.clientHeight -
          parseFloat(style.paddingTop) -
          parseFloat(style.paddingBottom) >
        parseFloat(style.lineHeight) * 2
      );
    }),
  ).toBe(true);
  await code.screenshot({
    path: "../../../.amp/in/artifacts/conversation-markdown-code-wrapped.png",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await first.getByLabel(/来源信息与查询定位/).click();
  await expect(first.getByLabel("文本")).toContainText("instructions-b");
  await first.locator(".message-provenance").screenshot({
    path: "../../../.amp/in/artifacts/conversation-markdown-provenance.png",
  });
});

test("footnotes navigate within their own message without replacing the conversation route", async ({
  page,
}) => {
  // Different comments reuse footnote labels, and one comment can cite the same note twice.
  const items = ["first", "second"].map((key) => {
    const body = `正文 ${key}[^同名]，再次引用[^同名]。\n\n[^同名]: ${key} 的脚注内容。`;
    return {
      ...activity,
      key,
      previous_version: null,
      body,
      body_length: Array.from(body).length,
      url:
        key === "first"
          ? "https://github.com/owner/agentflow/issues/85#issuecomment-first"
          : null,
    };
  });
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items,
        snapshot: 2,
        total: items.length,
        has_more: false,
        next_cursor: null,
        sources: [],
        changes: {},
      },
    }),
  );
  await page.goto("/#flow/42/conversation");
  for (const key of ["first", "second"]) {
    const body = page.locator(`article[data-entry='${key}'] .markdown-body`);
    const refs = body.locator("a[data-footnote-ref]");
    const note = body.locator("section[data-footnotes] li");
    const backrefs = note.locator("a[data-footnote-backref]");
    await expect(refs).toHaveCount(2);
    await expect(backrefs).toHaveCount(2);
    await expect(note).toContainText(`${key} 的脚注内容。`);
    const labelId = await body
      .locator("section[data-footnotes] h2")
      .getAttribute("id");
    if (!labelId) throw new Error("Footnotes have no accessible label target");
    for (let index = 0; index < 2; index++) {
      const ref = refs.nth(index);
      const refId = await ref.getAttribute("id");
      expect(refId).toBeTruthy();
      await expect(ref).toHaveAttribute("aria-describedby", labelId);
      await expect(backrefs.nth(index)).toHaveAttribute("href", `#${refId}`);
      await ref.click();
      await expect(note).toBeFocused();
      await expect(page).toHaveURL(/#flow\/42\/conversation$/);
      await backrefs.nth(index).press("Enter");
      await expect(ref).toBeFocused();
      await expect(page).toHaveURL(/#flow\/42\/conversation$/);
      // A backlink must not remove its reference from native keyboard traversal.
      await page.keyboard.press("Tab");
      await expect(index === 0 ? refs.nth(1) : backrefs.first()).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(ref).toBeFocused();
    }
    await refs.first().click({ button: "middle" });
  }
  const ids = await page
    .locator(".markdown-body [id]")
    .evaluateAll((elements) => elements.map((element) => element.id));
  expect(new Set(ids).size).toBe(ids.length);
  expect(page.context().pages()).toHaveLength(1);
});

test("footnote labels cannot collide with repeated-reference suffixes", async ({
  page,
}) => {
  // The second citation of `a` and the first citation of `a-2` previously shared an ID.
  const body = "A[^a] B[^a] C[^a-2]\n\n[^a]: 第一条脚注\n[^a-2]: 第二条脚注";
  await page.route("**/api/flows/42/activity?**", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...activity,
            previous_version: null,
            body,
            body_length: Array.from(body).length,
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
  await page.goto("/#flow/42/conversation");
  const message = page.locator(
    "article[data-entry='comment-1'] .markdown-body",
  );
  const refs = message.locator("a[data-footnote-ref]");
  const notes = message.locator("section[data-footnotes] li");
  await expect(refs).toHaveText(["1", "1", "2"]);
  await expect(notes.nth(0)).toContainText("第一条脚注");
  await expect(notes.nth(1)).toContainText("第二条脚注");
  const ids = await message
    .locator("[id]")
    .evaluateAll((elements) => elements.map((element) => element.id));
  expect(new Set(ids).size).toBe(ids.length);
  for (let index = 0; index < 3; index++) {
    const ref = refs.nth(index);
    const note = notes.nth(index === 2 ? 1 : 0);
    await ref.click();
    await expect(note).toBeFocused();
    await note
      .locator("a[data-footnote-backref]")
      .nth(index === 1 ? 1 : 0)
      .press("Enter");
    await expect(ref).toBeFocused();
    await expect(page).toHaveURL(/#flow\/42\/conversation$/);
  }
});

test("consecutive actor operations collapse across pages and sources without hiding messages", async ({
  page,
}) => {
  // A bot's Issue cross-reference and PR label can straddle pages; source changes must not split them.
  const check = {
    ...activity,
    source: "commit.check_runs:head",
    kind: "check",
    author: "GitHub Actions",
    body: "",
    body_length: 0,
    previous_version: null,
  };
  const checks = [
    ["Images", "publish", "skipped"],
    ["Operations validation", "recovery", "success"],
    ["Images", "build", "success"],
    ["Kubernetes templates", "render", "success"],
    ["CI", "pytest", "failure"],
    ["CI", "Ruff", "success"],
    ["CI", "Frontend", "success"],
    ["CI", "Mypy", "success"],
    ["CI", "Build", "success"],
  ].map(([workflow, title, conclusion], index) => {
    const body = conclusion === "failure" ? "测试失败，请检查日志。" : "";
    return {
      ...check,
      key: `check-${index}`,
      title,
      body,
      body_length: Array.from(body).length,
      occurred_at: new Date(Date.parse(now) + index * 1000).toISOString(),
      url: `https://example.test/checks/${index}`,
      metadata: { workflow_name: workflow, status: "completed", conclusion },
    };
  });
  const event = {
    ...activity,
    source: "pr.timeline",
    kind: "event",
    author: "b1indsight-bot[bot]",
    body: "",
    body_length: 0,
    previous_version: null,
  };
  const items = [
    ...checks,
    {
      ...event,
      key: "cross-reference",
      source: "issue.timeline",
      title: "cross-referenced",
      occurred_at: "2026-10-24T21:02:30Z",
      url: "https://github.com/b1indsight/agentflow/issues/88",
      metadata: { event: "cross-referenced" },
    },
    {
      ...event,
      key: "label",
      title: "labeled",
      occurred_at: "2026-10-24T21:02:32Z",
      url: "https://github.com/b1indsight/agentflow/pull/113",
      metadata: { event: "labeled", label: "flow:in-review" },
    },
    {
      ...activity,
      key: "message",
      source: check.source,
      author: check.author,
      occurred_at: "2026-10-24T21:02:33Z",
      previous_version: null,
    },
    {
      ...checks[0],
      key: "after-message",
      occurred_at: "2026-10-24T21:02:34Z",
    },
    {
      ...checks[1],
      key: "other-commit",
      source: "commit.check_runs:other",
      occurred_at: "2026-10-24T21:02:35Z",
    },
  ];
  await page.route("**/api/flows/42/activity?**", (route) => {
    const later = new URL(route.request().url()).searchParams.has("cursor");
    return route.fulfill({
      json: {
        items: later ? items.slice(10) : items.slice(0, 10),
        snapshot: 2,
        total: items.length,
        has_more: !later,
        next_cursor: later ? null : "operations",
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const group = page.getByRole("button", {
    name: "操作记录 · 9 条",
    exact: true,
  });
  const events = page
    .locator("article[data-activity-summary='cross-reference']")
    .getByRole("button", { name: "操作记录 · 2 条", exact: true });
  await expect(group).toHaveAttribute("aria-expanded", "false");
  await expect(events).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByText("CI: pytest: failure", { exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByText(checks[4].body, { exact: true }),
  ).not.toBeVisible();
  await expect(page.getByText(activity.body, { exact: true })).toBeVisible();
  await expect(
    page
      .locator("article[data-activity-summary='after-message']")
      .getByRole("button", { name: "操作记录 · 2 条", exact: true }),
  ).toHaveAttribute("aria-expanded", "false");
  for (const key of ["after-message", "other-commit"]) {
    const row = page.locator(`article[data-entry='${key}']`);
    await expect(row.locator(".event-action")).not.toBeVisible();
  }
  const capture = async (state: string) => {
    const viewport = page.viewportSize()!;
    // Element screenshots cannot capture content clipped by the shared scroll root.
    await page.setViewportSize({ ...viewport, height: 2000 });
    await page.locator(".conversation-stream").screenshot({
      path: `../../../.amp/in/artifacts/activity-operations-cross-source-${state}.png`,
    });
    await page.setViewportSize(viewport);
  };
  await capture("desktop-collapsed");
  await group.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText(checks[4].body, { exact: true })).toBeVisible();
  for (const [index, check] of checks.entries()) {
    const row = page.locator(`article[data-entry='${check.key}']`);
    await expect(row.locator(".event-action")).toHaveText(
      `${check.metadata.workflow_name}: ${check.title}: ${check.metadata.conclusion}`,
    );
    await expect(row.locator("time:visible")).toHaveText(
      await page.evaluate(
        (date) => new Date(date).toLocaleString(),
        check.occurred_at,
      ),
    );
    await expect(
      row.getByRole("link", { name: "在 GitHub 中查看" }),
    ).toHaveAttribute("href", `https://example.test/checks/${index}`);
  }
  await expect(
    page.getByText("cross-referenced", { exact: true }),
  ).not.toBeVisible();
  await events.click();
  await expect(
    page.getByText("cross-referenced", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("添加标签：flow:in-review", { exact: true }),
  ).toBeVisible();
  for (const [key, url] of [
    ["cross-reference", "https://github.com/b1indsight/agentflow/issues/88"],
    ["label", "https://github.com/b1indsight/agentflow/pull/113"],
  ]) {
    await expect(
      page
        .locator(`article[data-entry='${key}']`)
        .getByRole("link", { name: "在 GitHub 中查看" }),
    ).toHaveAttribute("href", url);
  }
  await capture("desktop-expanded");
  await events.click();
  await page.getByRole("tab", { name: "概览" }).click();
  await page.getByRole("tab", { name: "会话" }).click();
  await expect(group).toHaveAttribute("aria-expanded", "false");
  await expect(
    page.getByText("CI: pytest: failure", { exact: true }),
  ).not.toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await capture("mobile-collapsed");
  await group.click();
  await expect(
    page.getByText("CI: pytest: failure", { exact: true }),
  ).toBeVisible();
  await events.click();
  await expect(
    page.getByText("cross-referenced", { exact: true }),
  ).toBeVisible();
  await capture("mobile-expanded");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
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
  await expect(page.locator("[data-activity-summary='orphan']")).toBeAttached();
  await expect(page.getByText("添加标签：flow", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: /^(message|tool_result|exec|labeled)$/ }),
  ).toHaveCount(0);
  await expect(
    page.locator("[data-activity-summary='orphan']"),
  ).not.toHaveClass(/continuation/);
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
  await expect(page.locator(".tool-activity-body pre")).toHaveText([
    "exec git status --short",
    "工作目录干净",
    "test pytest",
    "测试通过",
  ]);
  const callA = page.locator("article[data-entry='call-a']");
  const callB = page.locator("article[data-entry='call-b']");
  await expect(callA).toHaveClass(/continuation/);
  await expect(
    page.locator("article[data-entry='result-a'] blockquote"),
  ).toHaveText("工作目录干净");
  await expect(
    page.locator("article[data-entry='result-b'] blockquote"),
  ).toHaveText("测试通过");
  await expect(page.locator(".tool-activity-body small")).toHaveCount(0);
  await expect(callA.locator(".tool-activity-body")).not.toContainText(
    "call-a",
  );
  await expect(callB.locator(".tool-activity-body")).not.toContainText(
    "call-b",
  );
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
  await expect(tools).toHaveAttribute("aria-expanded", "false");
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
      .locator("article[data-entry='next-result']")
      .getByText("下一轮的目录", { exact: true }),
  ).toBeVisible();
  await page
    .locator("[data-activity-summary='orphan']")
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

for (const kind of ["tool", "operation"] as const) {
  test(`folded ${kind} history stops automatic continuation and reads one page per explicit click`, async ({
    page,
  }) => {
    // A long worker turn or bot-event burst adds no visible rows when another page only extends its summary.
    const errors: string[] = [],
      continuations: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let permittedStart = 20;
    let releaseUnexpected!: () => void;
    const unexpectedPage = new Promise<void>((resolve) => {
      releaseUnexpected = resolve;
    });
    await page.route("**/api/flows/42/activity?**", async (route) => {
      const url = new URL(route.request().url());
      const start = Number(url.searchParams.get("cursor") ?? 0);
      if (start)
        continuations.push(`${start}:${url.searchParams.get("snapshot")}`);
      // Keep an unwanted continuation in flight so a runaway chain cannot hide behind an unchanged summary.
      if (start > permittedStart) await unexpectedPage;
      return route.fulfill({
        json: {
          items: Array.from({ length: Math.min(20, 120 - start) }, (_, i) => {
            const n = start + i;
            const body = kind === "tool" ? `折叠工具记录 ${n}` : "";
            return {
              ...activity,
              key: `folded-${n}`,
              source:
                kind === "tool"
                  ? "worker"
                  : n % 2
                    ? "pr.timeline"
                    : "issue.timeline",
              kind:
                kind === "tool"
                  ? n % 2
                    ? "tool_result"
                    : "tool_call"
                  : "event",
              author:
                kind === "tool" ? (n % 2 ? "tool" : "assistant") : "bot[bot]",
              title:
                kind === "tool" ? (n % 2 ? "tool_result" : "exec") : "labeled",
              occurred_at: new Date(Date.parse(now) + n * 1000).toISOString(),
              body,
              body_length: body.length,
              previous_version: null,
              metadata:
                kind === "tool"
                  ? {
                      attempt_id: 1,
                      session_id: "folded-history",
                      session_generation: 0,
                      call_id: `call-${Math.floor(n / 2)}`,
                    }
                  : { event: "labeled", label: `archive-${n}` },
            };
          }),
          snapshot: 2,
          total: 120,
          next_cursor: start + 20 < 120 ? String(start + 20) : null,
          has_more: start + 20 < 120,
          sources: [],
          changes: {},
        },
      });
    });
    try {
      await page.goto("/#flow/42/conversation");
      const summary = page.getByRole("button", {
        name: kind === "tool" ? /^工具活动/ : /^操作记录/,
      });
      const more = page.getByRole("button", {
        name: "读取更多交互",
        exact: true,
      });
      for (const count of [40, 60, 80]) {
        if (count > 40) {
          permittedStart = count - 20;
          await more.click();
        }
        await expect(summary).toHaveAccessibleName(
          kind === "tool"
            ? `工具活动 · ${count / 2} 次调用 · ${count / 2} 条结果`
            : `操作记录 · ${count} 条`,
        );
        await expect(summary).toHaveAttribute("aria-expanded", "false");
        await expect(summary).toBeInViewport();
        // Let busy-state effects and IntersectionObserver delivery run again before checking the request bound.
        await page.evaluate(async () => {
          for (let frame = 0; frame < 18; frame++)
            await new Promise<void>((resolve) =>
              requestAnimationFrame(() => setTimeout(resolve, 0)),
            );
        });
        expect(continuations).toEqual(
          Array.from({ length: count / 20 - 1 }, (_, i) => `${(i + 1) * 20}:2`),
        );
        await expect(more).toBeVisible();
        if (count > 40) await expect(more).toBeFocused();
        await expect(
          page.locator('article[data-entry^="folded-"]'),
        ).toHaveCount(0);
      }
      expect(errors).toEqual([]);
    } finally {
      releaseUnexpected();
    }
  });
}

for (const change of [
  "prepending a member",
  "replacing the old first member",
] as const) {
  test(`snapshot refresh preserves visible expanded tool bodies and offsets after ${change}`, async ({
    page,
  }) => {
    // Archive backfills can precede the output being read, and a stale first record can disappear.
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const records = Array.from({ length: 52 }, (_, n) => {
      const unrelated = n >= 12 && n < 20;
      const main = n >= 20 && n < 36;
      const tool = unrelated || main;
      const member = unrelated ? n - 12 : n - 20;
      const session = unrelated ? "refresh-unrelated" : "refresh-main";
      const body = tool
        ? `${unrelated ? "无关工具正文" : "刷新工具正文"} ${member}\n${"刷新时保留正在阅读的工具输出。\n".repeat(5)}`
        : `刷新普通正文 ${n}。${"保留本行阅读位置。".repeat(4)}`;
      return {
        ...activity,
        key: tool ? `${session}-${member}` : `refresh-message-${n}`,
        source: tool ? "worker" : activity.source,
        kind: tool ? (member % 2 ? "tool_result" : "tool_call") : "comment",
        author: tool
          ? member % 2
            ? "tool"
            : "assistant"
          : n % 2
            ? "reviewer"
            : "owner",
        title: tool ? (member % 2 ? "tool_result" : "exec") : "评论",
        // Numeric key suffixes are not archive ordering; every record has its real ascending time.
        occurred_at: new Date(Date.parse(now) + n * 1000).toISOString(),
        body,
        body_length: body.length,
        previous_version: null,
        metadata: tool
          ? {
              attempt_id: 1,
              session_id: session,
              session_generation: 0,
              call_id: `${session}-call-${Math.floor(member / 2)}`,
            }
          : {},
      };
    });
    const original = records.filter((_, n) => n < 16 || n >= 20);
    const body = "补采的更早工具调用\n保留后面的可见工具正文。";
    const earlier = {
      ...records[20],
      key: "refresh-earlier",
      occurred_at: new Date(Date.parse(now) + 19500).toISOString(),
      body,
      body_length: body.length,
      metadata: { ...records[20].metadata, call_id: "backfilled-call" },
    };
    const removeFirst = change === "replacing the old first member";
    // Extra unrelated records occupy the main group's former raw indexes; they must not inherit its expansion.
    const refreshed = [
      ...records.slice(0, 20),
      earlier,
      ...records.slice(removeFirst ? 21 : 20),
    ];
    await page.route("**/api/flows/42/activity?**", (route) => {
      const url = new URL(route.request().url());
      const snapshot =
        url.searchParams.get("refresh") === "true"
          ? 3
          : Number(url.searchParams.get("snapshot") ?? 2);
      const start = Number(url.searchParams.get("cursor") ?? 0);
      const items = snapshot === 3 ? refreshed : original;
      return route.fulfill({
        json: {
          items: items
            .slice(start, start + 20)
            .map((entry) => ({ ...entry, version: snapshot })),
          snapshot,
          total: items.length,
          next_cursor: start + 20 < items.length ? String(start + 20) : null,
          has_more: start + 20 < items.length,
          sources: [],
          changes:
            snapshot === 3 ? { added: 5, removed: removeFirst ? 1 : 0 } : {},
        },
      });
    });
    await page.goto("/#flow/42/conversation");
    const root = page.locator(".detail-body");
    await expect
      .poll(async () => {
        await root.evaluate((node) => {
          node.scrollTop = node.scrollHeight;
        });
        return page
          .getByRole("button", {
            name: "已到当前本地快照末尾 · 点击刷新",
            exact: true,
          })
          .isVisible();
      })
      .toBe(true);
    const main = page.getByRole("button", {
      name: "工具活动 · 8 次调用 · 8 条结果",
      exact: true,
    });
    await expect(
      page.getByRole("button", {
        name: "工具活动 · 2 次调用 · 2 条结果",
        exact: true,
      }),
    ).toHaveAttribute("aria-expanded", "false");
    await main.click();
    const target = page.locator('article[data-entry="refresh-main-8"]');
    await expect(target.locator("pre")).toContainText("刷新工具正文 8");
    const before = await target.evaluate(async (entry) => {
      const node = entry.closest<HTMLElement>("[data-scroll-root]")!;
      const row = entry.closest("[data-window-row]")!;
      const previous = node
        .querySelector('[data-entry="refresh-main-7"]')!
        .closest("[data-window-row]")!;
      node.scrollTop +=
        row.getBoundingClientRect().top -
        node.getBoundingClientRect().top -
        node.clientTop -
        previous.getBoundingClientRect().height / 2;
      for (let frame = 0; frame < 6; frame++)
        await new Promise(requestAnimationFrame);
      const top = node.getBoundingClientRect().top + node.clientTop;
      const first = [
        ...node.querySelectorAll(".conversation-stream [data-window-row]"),
      ].find((item) => {
        const bounds = item.getBoundingClientRect();
        return bounds.top >= top && bounds.bottom <= top + node.clientHeight;
      });
      return {
        offset: row.getBoundingClientRect().top - top,
        first: first?.querySelector<HTMLElement>("article")?.dataset.entry,
      };
    });
    expect(before.first).toBe("refresh-main-8");
    await expect(
      page.locator('article[data-entry="refresh-main-0"]'),
    ).not.toBeInViewport();
    await page.getByRole("button", { name: "刷新当前视图" }).click();
    await expect(
      page.getByRole("status").filter({ hasText: "快照更新" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: `工具活动 · ${removeFirst ? 8 : 9} 次调用 · 8 条结果`,
        exact: true,
      }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(target.locator("pre")).toContainText("刷新工具正文 8");
    await expect(target).toBeInViewport();
    await expect
      .poll(() =>
        target.evaluate((entry, offset) => {
          const node = entry.closest<HTMLElement>("[data-scroll-root]")!;
          return Math.abs(
            entry.closest("[data-window-row]")!.getBoundingClientRect().top -
              node.getBoundingClientRect().top -
              node.clientTop -
              offset,
          );
        }, before.offset),
      )
      .toBeLessThan(2);
    await expect(
      page.getByRole("button", {
        name: "工具活动 · 4 次调用 · 4 条结果",
        exact: true,
      }),
    ).toHaveAttribute("aria-expanded", "false");
    await expect(
      page.locator('article[data-entry^="refresh-unrelated-"]'),
    ).toHaveCount(0);
    if (removeFirst)
      await expect(
        page.locator('article[data-entry="refresh-main-0"]'),
      ).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

for (const reading of [
  "tool body",
  "tall tool body",
  "following message",
] as const) {
  test(`conversation tab re-entry folds groups and restores the ${reading} logical offset in a virtual list`, async ({
    page,
  }) => {
    test.setTimeout(45000);
    // Leaving a long conversation folds its tools; restoring old pixels would land on unrelated later messages.
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const tall = reading === "tall tool body";
    const records = Array.from({ length: 200 }, (_, n) => {
      const tool = n >= 90 && n < 110;
      const body = tool
        ? `重返工具正文 ${n}\n${"切换标签页后继续阅读同一位置。\n".repeat(tall && n === 96 ? 100 : 5)}`
        : `重返普通正文 ${n}。${"前后消息让折叠后的目标偏移仍然可达。".repeat(3)}`;
      return {
        ...activity,
        key: `reentry-${n}`,
        source: tool ? "worker" : activity.source,
        kind: tool ? (n % 2 ? "tool_result" : "tool_call") : "comment",
        author: tool
          ? n % 2
            ? "tool"
            : "assistant"
          : n % 2
            ? "reviewer"
            : "owner",
        title: tool ? (n % 2 ? "tool_result" : "exec") : "评论",
        occurred_at: new Date(Date.parse(now) + n * 1000).toISOString(),
        body,
        body_length: body.length,
        previous_version: null,
        metadata: tool
          ? {
              attempt_id: 1,
              session_id: "tab-reentry",
              session_generation: 0,
              call_id: `call-${Math.floor(n / 2)}`,
            }
          : {},
      };
    });
    await page.route("**/api/flows/42/activity?**", (route) => {
      const start = Number(
        new URL(route.request().url()).searchParams.get("cursor") ?? 0,
      );
      return route.fulfill({
        json: {
          items: records.slice(start, start + 20),
          snapshot: 2,
          total: records.length,
          next_cursor: start + 20 < records.length ? String(start + 20) : null,
          has_more: start + 20 < records.length,
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
          await root.evaluate((node) => {
            node.scrollTop = node.scrollHeight;
          });
          return page
            .getByRole("button", {
              name: "已到当前本地快照末尾 · 点击刷新",
              exact: true,
            })
            .isVisible();
        },
        { timeout: 15000 },
      )
      .toBe(true);
    await expect(list).toHaveAttribute("data-window-count", "181");
    await expect(list).toHaveClass(/virtual-list/);
    await root.evaluate((node) => {
      node.scrollTop = 0;
    });
    const bar = page.getByRole("button", {
      name: "工具活动 · 10 次调用 · 10 条结果",
      exact: true,
    });
    // Both targets are known but unmounted, not merely absent because their API pages are unread.
    await expect(bar).toHaveCount(0);
    await expect(page.locator('article[data-entry="reentry-110"]')).toHaveCount(
      0,
    );
    expect(
      await list.locator(":scope > [data-window-row]").count(),
    ).toBeLessThanOrEqual(60);
    await expect
      .poll(
        async () => {
          if (!(await bar.count()))
            await root.evaluate((node) => {
              node.scrollTop += node.clientHeight;
            });
          return bar.count();
        },
        { timeout: 15000, intervals: [100] },
      )
      .toBe(1);
    await bar.click();
    await expect(bar).toHaveAttribute("aria-expanded", "true");
    await expect(list).toHaveAttribute("data-window-count", "201");
    const targetIndex = reading === "following message" ? 110 : 96;
    const target = page.locator(`article[data-entry="reentry-${targetIndex}"]`);
    await expect
      .poll(
        async () => {
          if (!(await target.count()))
            await root.evaluate((node) => {
              node.scrollTop += node.clientHeight / 2;
            });
          return target.count();
        },
        { timeout: 15000, intervals: [100] },
      )
      .toBe(1);
    const before = await target.evaluate(
      async (entry, { index, tall }) => {
        const node = entry.closest<HTMLElement>("[data-scroll-root]")!;
        const row = entry.closest("[data-window-row]")!;
        const previous = node
          .querySelector(`[data-entry="reentry-${index - 1}"]`)!
          .closest("[data-window-row]")!;
        // A tall body leaves no complete row; otherwise half the preceding tool remains visible.
        const offset = tall
          ? -node.clientHeight / 2
          : previous.getBoundingClientRect().height / 2;
        node.scrollTop +=
          row.getBoundingClientRect().top -
          node.getBoundingClientRect().top -
          node.clientTop -
          offset;
        for (let frame = 0; frame < 6; frame++)
          await new Promise(requestAnimationFrame);
        const top = node.getBoundingClientRect().top + node.clientTop;
        const rows = [
          ...node.querySelectorAll(".conversation-stream [data-window-row]"),
        ];
        const first =
          rows.find((item) => {
            const bounds = item.getBoundingClientRect();
            return (
              bounds.top >= top && bounds.bottom <= top + node.clientHeight
            );
          }) ??
          rows.find((item) => {
            const bounds = item.getBoundingClientRect();
            return bounds.bottom > top && bounds.top < top + node.clientHeight;
          });
        return {
          offset: row.getBoundingClientRect().top - top,
          first: first?.querySelector<HTMLElement>("article")?.dataset.entry,
          previousTop: previous.getBoundingClientRect().top - top,
          previousBottom: previous.getBoundingClientRect().bottom - top,
        };
      },
      { index: targetIndex, tall },
    );
    expect(before.first).toBe(`reentry-${targetIndex}`);
    expect(before.previousTop).toBeLessThan(0);
    if (tall) expect(before.previousBottom).toBeLessThanOrEqual(0);
    else expect(before.previousBottom).toBeGreaterThan(0);
    await expect(target).toBeInViewport();
    await expect(bar).toHaveAttribute("aria-expanded", "true");
    await page.getByRole("tab", { name: "概览" }).click();
    await expect(page.getByRole("heading", { name: "任务信息" })).toBeVisible();
    await page.getByRole("tab", { name: "会话" }).click();
    await expect(list).toHaveAttribute("data-window-count", "181");
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await expect(list.locator(".tool-activity-body")).toHaveCount(0);
    const restored = reading === "following message" ? target : bar;
    // A short summary must be visible even when the former body began above the viewport.
    const expectedOffset = tall ? 0 : before.offset;
    await expect(restored).toBeInViewport();
    await expect
      .poll(() =>
        restored.evaluate((entry, offset) => {
          const node = entry.closest<HTMLElement>("[data-scroll-root]")!;
          return Math.abs(
            entry.closest("[data-window-row]")!.getBoundingClientRect().top -
              node.getBoundingClientRect().top -
              node.clientTop -
              offset,
          );
        }, expectedOffset),
      )
      .toBeLessThan(2);
    const frames = await restored.evaluate(async (entry) => {
      const node = entry.closest<HTMLElement>("[data-scroll-root]")!;
      const row = entry.closest("[data-window-row]")!;
      const offsets: number[] = [];
      let mounted = 0;
      for (let frame = 0; frame < 18; frame++) {
        await new Promise<void>((resolve) =>
          requestAnimationFrame(() => setTimeout(resolve, 0)),
        );
        offsets.push(
          row.getBoundingClientRect().top -
            node.getBoundingClientRect().top -
            node.clientTop,
        );
        mounted = Math.max(
          mounted,
          node.querySelectorAll(".conversation-stream [data-window-row]")
            .length,
        );
      }
      return {
        offsets,
        mounted,
        scrollTop: node.scrollTop,
        maxScroll: node.scrollHeight - node.clientHeight,
      };
    });
    expect(
      Math.max(
        ...frames.offsets.map((offset) => Math.abs(offset - expectedOffset)),
      ),
    ).toBeLessThan(2);
    expect(frames.mounted).toBeLessThanOrEqual(60);
    // Prefix and suffix messages keep the expected offset away from unavoidable boundary clamping.
    expect(frames.scrollTop).toBeGreaterThan(0);
    expect(frames.scrollTop).toBeLessThan(frames.maxScroll);
    expect(errors).toEqual([]);
  });
}

test("long tool activity remains bounded and readable across virtual windows", async ({
  page,
}) => {
  // The group outlives the 240-body cache without changing statistics or fetching bodies while folded.
  const errors: string[] = [],
    rereads: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/flows/42/activity?**", (route) => {
    const url = new URL(route.request().url());
    const start = Number(url.searchParams.get("cursor") ?? 0);
    rereads.push(`${start}:${url.searchParams.get("snapshot")}`);
    return route.fulfill({
      json: {
        items: Array.from({ length: Math.min(20, 321 - start) }, (_, index) => {
          const n = start + index;
          const body = n === 320 ? "本轮全部完成" : `工具记录 ${n}`;
          return {
            ...activity,
            key: `tool-run-${n}`,
            source: "worker",
            author: n % 2 ? "tool" : "assistant",
            kind: n === 320 ? "message" : n % 2 ? "tool_result" : "tool_call",
            title: n === 320 ? "message" : n % 2 ? "tool_result" : "exec",
            body,
            body_length: body.length,
            previous_version: null,
            metadata: {
              attempt_id: 1,
              session_id: "long-tools",
              session_generation: 0,
              call_id: `call-${Math.floor(n / 2)}`,
              archive_locator: `tool-source-${n}`,
            },
          };
        }),
        snapshot: 2,
        total: 321,
        next_cursor: start + 20 < 321 ? String(start + 20) : null,
        has_more: start + 20 < 321,
        sources: [],
        changes: {},
      },
    });
  });
  await page.goto("/#flow/42/conversation");
  const root = page.locator(".detail-body");
  const list = page.locator('[data-window-list="会话活动"]');
  const more = page.getByRole("button", {
    name: "读取更多交互",
    exact: true,
  });
  await expect
    .poll(
      async () => {
        await root.evaluate((e) => {
          e.scrollTop = e.scrollHeight;
        });
        // Reading the entire folded archive here is intentional, unlike automatic filling.
        if (await more.isVisible()) await more.click();
        return page
          .getByRole("button", {
            name: "已到当前本地快照末尾 · 点击刷新",
            exact: true,
          })
          .isVisible();
      },
      { timeout: 15000 },
    )
    .toBe(true);
  await expect(list).toHaveAttribute("data-window-count", "2");
  await root.evaluate((e) => {
    e.scrollTop = 0;
  });
  const bar = page.getByRole("button", { name: /^工具活动/ }).first();
  await expect(bar).toHaveAccessibleName("工具活动 · 160 次调用 · 160 条结果");
  await expect(bar).toHaveAttribute("aria-expanded", "false");
  expect(rereads.filter((read) => read === "0:2")).toHaveLength(0);
  await bar.click();
  await expect(page.getByText("工具记录 0", { exact: true })).toBeVisible();
  expect(rereads.filter((read) => read === "0:2").length).toBeGreaterThan(0);
  const source = page
    .locator('article[data-entry="tool-run-0"]')
    .getByRole("button", {
      name: "来源信息与查询定位 · 321 条记录",
      exact: true,
    });
  await source.click();
  await expect(page.getByRole("group", { name: "来源记录分页" })).toContainText(
    "记录 1–20 / 321",
  );
  await page
    .getByRole("button", { name: "下一页来源记录", exact: true })
    .click();
  await expect(page.getByRole("group", { name: "来源记录分页" })).toContainText(
    "记录 21–40 / 321",
  );
  await expect(
    page.locator('article[data-entry="tool-run-0"] .message-provenance'),
  ).toContainText('"archive_locator": "tool-source-20"');
  await source.click();
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
        (await page.getByText("工具记录 319", { exact: true }).isVisible()) &&
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

for (const [expandedRows, width] of [
  [59, 1440],
  [60, 1440],
  [61, 1440],
  [61, 390],
]) {
  test(`whole-group visibility and collapse preserve reading at ${expandedRows} rows, ${width}px`, async ({
    page,
  }) => {
    // A prefix makes the replacement summary's inherited offset reachable after removing the anchor.
    await page.setViewportSize({ width, height: 900 });
    const records = Array.from({ length: expandedRows - 1 }, (_, n) => {
      const tool = n >= 12 && n < 32;
      const body = tool
        ? `工具正文 ${n}\n${"保持工具输出可读。\n".repeat(5)}`
        : `普通正文 ${n}。${"保留本行阅读位置。".repeat(5)}`;
      return {
        ...activity,
        key: `visibility-${n}`,
        source: tool ? "worker" : activity.source,
        kind: tool ? (n % 2 ? "tool_result" : "tool_call") : "comment",
        author: tool ? (n % 2 ? "tool" : "assistant") : "owner",
        title: tool ? "exec" : "评论",
        body,
        body_length: body.length,
        previous_version: null,
        metadata: tool
          ? {
              attempt_id: 1,
              session_id: "visibility",
              session_generation: 0,
              call_id: `visibility-call-${Math.floor(n / 2)}`,
            }
          : {},
      };
    });
    await page.route("**/api/flows/42/activity?**", (route) => {
      const start = Number(
        new URL(route.request().url()).searchParams.get("cursor") ?? 0,
      );
      return route.fulfill({
        json: {
          items: records.slice(start, start + 20),
          snapshot: 2,
          total: records.length,
          next_cursor: start + 20 < records.length ? String(start + 20) : null,
          has_more: start + 20 < records.length,
          sources: [],
          changes: {},
        },
      });
    });
    await page.goto("/#flow/42/conversation");
    const root = page.locator(".detail-body");
    const list = page.locator('[data-window-list="会话活动"]');
    await expect
      .poll(async () => {
        await root.evaluate((node) => {
          node.scrollTop = node.scrollHeight;
        });
        return page
          .getByRole("button", {
            name: "已到当前本地快照末尾 · 点击刷新",
            exact: true,
          })
          .isVisible();
      })
      .toBe(true);
    const bar = page.getByRole("button", {
      name: "工具活动 · 10 次调用 · 10 条结果",
      exact: true,
    });
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await bar.click();
    await expect(list).toHaveAttribute(
      "data-window-count",
      String(expandedRows),
    );
    await expect(bar).toBeFocused();
    await expect(page.getByText(/^工具正文 12/)).toBeVisible();
    await bar.evaluate((button) => {
      const root = button.closest<HTMLElement>("[data-scroll-root]")!;
      const row = button.closest("[data-window-row]")!;
      root.scrollTop +=
        row.getBoundingClientRect().bottom -
        root.getBoundingClientRect().top +
        30;
    });
    await expect(bar).not.toBeInViewport();
    await expect(bar).toHaveAttribute("aria-expanded", "true");
    const offset = await root.evaluate((node) => {
      const top = node.getBoundingClientRect().top + node.clientTop;
      const bottom = Math.min(top + node.clientHeight, innerHeight);
      const first = [...node.querySelectorAll("[data-window-row]")].find(
        (row) => {
          const rect = row.getBoundingClientRect();
          return rect.height > 0 && rect.top >= top && rect.bottom <= bottom;
        },
      )!;
      return first.getBoundingClientRect().top - top;
    });
    // Enter on the still-focused, offscreen summary must not scroll it into view before collapsing.
    await page.keyboard.press("Enter");
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await expect
      .poll(() =>
        bar.evaluate((button, offset) => {
          const root = button.closest<HTMLElement>("[data-scroll-root]")!;
          return Math.abs(
            button.closest("[data-window-row]")!.getBoundingClientRect().top -
              root.getBoundingClientRect().top -
              root.clientTop -
              offset,
          );
        }, offset),
      )
      .toBeLessThan(2);
    await bar.click();
    await expect(bar).toHaveAttribute("aria-expanded", "true");
    await root.evaluate((node) => {
      node.scrollTop = node.scrollHeight;
    });
    // Focus pins and overscan are not visibility: all member bodies are now above the viewport.
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await expect(
      page.getByText(new RegExp(`^普通正文 ${records.length - 1}。`)),
    ).toBeVisible();
    await bar.scrollIntoViewIfNeeded();
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await bar.click();
    await expect(bar).toHaveAttribute("aria-expanded", "true");
    await root.evaluate((node) => {
      node.scrollTop = 0;
    });
    // The same reset applies when the entire group is below the viewport, not only above it.
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByText(/^普通正文 0。/)).toBeInViewport();
    await bar.scrollIntoViewIfNeeded();
    await expect(bar).toHaveAttribute("aria-expanded", "false");
    expect(
      await list.locator(":scope > [data-window-row]").count(),
    ).toBeLessThanOrEqual(60);
    expect(
      await list.locator('.message-body, pre[aria-label="工具摘要"]').count(),
    ).toBeLessThanOrEqual(60);
  });
}

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
