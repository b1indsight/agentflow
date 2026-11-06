import { expect, test, type Page, type Route } from "@playwright/test";
import type {
  ActivityItem,
  EntryReading,
  ReadObservation,
  ReadingAnchor,
  ReadingProgress,
  ReadingResume,
} from "../types";

const now = "2026-11-05T00:00:00Z";
const fresh = (): EntryReading => ({
  generation: 0,
  header_read: false,
  ranges: [],
  explicit_read: false,
  unread: true,
});
const item = (key: string, body = "甲😀乙丙丁戊"): ActivityItem => ({
  key,
  version: 1,
  read_revision: 1,
  reading: fresh(),
  source: "issue.comments",
  kind: "comment",
  author: "owner",
  title: key,
  body: Array.from(body).slice(0, 2000).join(""),
  body_length: Array.from(body).length,
  occurred_at: now,
  updated_at: now,
  observed_at: now,
  deleted: false,
  prior: false,
  previous_version: null,
  parent_key: null,
  metadata: {},
  url: null,
});
const flow = {
  id: 42,
  repo: "owner/repo",
  issue_number: 88,
  issue_url: "https://example.test/88",
  title: "个人阅读",
  pr_number: null,
  pr_url: null,
  lifecycle: "active",
  phase: "running",
  action: "running",
  worker_id: null,
  worker_historical: false,
  revision_cycles: 0,
  branch: "flow/3",
  created_at: now,
  updated_at: now,
  archived_at: null,
  state_version: 1,
  reason: "",
  next_condition: "",
  sources: [],
};

function model(entries: ActivityItem[], bodies = new Map<string, string>()) {
  return {
    entries,
    bodies,
    observations: [] as ReadObservation[],
    marks: [] as string[],
    claims: 0,
    saves: 0,
    writer: "",
    failObservation: false,
    delayObservation: null as Promise<void> | null,
    delayPosition: null as Promise<void> | null,
    resumeReason: "exact" as ReadingResume["reason"],
    receipts: new Map<string, EntryReading>(),
    progress: {
      receipts_version: 0,
      position_revision: 0,
      writer_epoch: 0,
      anchor: null,
      snapshot: 1,
      unread_count: entries.length,
      total_count: entries.length,
      saved_at: null,
    } as ReadingProgress,
  };
}
type Model = ReturnType<typeof model>;
async function serve(page: Page, state: Model) {
  await page.addInitScript(() => {
    // Control focus/visibility independently of headless browser window scheduling.
    Object.defineProperty(document, "hasFocus", {
      value: () => document.documentElement.dataset.focus !== "off",
    });
    Object.defineProperty(document, "visibilityState", {
      get: () => document.documentElement.dataset.visibility ?? "visible",
    });
  });
  const attach = (entry: ActivityItem) => ({
    ...entry,
    reading: state.receipts.get(entry.key) ?? fresh(),
  });
  const pageOf = (start = 0) => ({
    items: state.entries.slice(start, start + 20).map(attach),
    snapshot: 1,
    total: state.entries.length,
    next_cursor: start + 20 < state.entries.length ? String(start + 20) : null,
    has_more: start + 20 < state.entries.length,
    sources: [],
    changes: {},
  });
  const respond = (route: Route) => {
    state.progress.unread_count = state.entries.filter(
      (e) => state.receipts.get(e.key)?.unread !== false,
    ).length;
    return route.fulfill({ json: state.progress });
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request(),
      url = new URL(request.url()),
      path = url.pathname;
    if (path === "/api/repos")
      return route.fulfill({
        json: { items: [flow.repo], next_cursor: null, has_more: false },
      });
    if (path === "/api/flows")
      return route.fulfill({
        json: {
          items:
            url.searchParams.get("phase") === "running"
              ? [{ ...flow, unread_count: state.progress.unread_count }]
              : [],
          counts: { running: 1, queued: 0, review: 0, blocked: 0, done: 0 },
          next_cursor: null,
          has_more: false,
        },
      });
    if (path === "/api/flows/42") return route.fulfill({ json: flow });
    if (path.endsWith("/activity"))
      return route.fulfill({
        json: pageOf(Number(url.searchParams.get("cursor") ?? 0)),
      });
    if (path.endsWith("/content")) {
      const key = url.searchParams.get("key")!,
        entry = state.entries.find((e) => e.key === key)!;
      const text = state.bodies.get(key) ?? entry.body,
        chars = Array.from(text);
      const offset = Number(url.searchParams.get("offset")),
        end = Math.min(
          chars.length,
          offset + Number(url.searchParams.get("limit")),
        );
      return route.fulfill({
        json: {
          key,
          version: 1,
          text: chars.slice(offset, end).join(""),
          total: chars.length,
          next_offset: end < chars.length ? end : null,
        },
      });
    }
    if (path.endsWith("/reading")) return respond(route);
    if (path.endsWith("/resume")) {
      const anchor = state.progress.anchor,
        index = anchor
          ? state.entries.findIndex((e) => e.key === anchor.key)
          : 0;
      return route.fulfill({
        json: {
          state: state.progress,
          original_anchor: anchor,
          resolved_anchor:
            anchor && state.resumeReason !== "exact"
              ? {
                  ...anchor,
                  part: "summary",
                  body_offset: 0,
                  block_fraction: 0,
                }
              : anchor,
          reason: state.resumeReason,
          page: pageOf(Math.max(0, index)),
          start_cursor: index > 0 ? String(index) : null,
          parent_key: null,
        },
      });
    }
    if (path.endsWith("/observations")) {
      if (state.failObservation) {
        state.failObservation = false;
        return route.abort("failed");
      }
      const items = request.postDataJSON().items as ReadObservation[];
      for (const observation of items) {
        const prior = state.receipts.get(observation.key) ?? fresh();
        if (prior.generation !== observation.generation)
          return route.fulfill({
            status: 409,
            json: { detail: "reading_conflict" },
          });
        state.observations.push(observation);
        const ranges = [...prior.ranges, ...observation.ranges].sort(
          (a, b) => a[0] - b[0],
        );
        const merged: number[][] = [];
        for (const [start, end] of ranges) {
          const last = merged.at(-1);
          if (last && start <= last[1]) last[1] = Math.max(end, last[1]);
          else merged.push([start, end]);
        }
        const entry = state.entries.find((e) => e.key === observation.key)!;
        const header_read = prior.header_read || observation.header;
        state.receipts.set(entry.key, {
          ...prior,
          ranges: merged,
          header_read,
          unread: !(
            prior.explicit_read ||
            (header_read &&
              (entry.body_length === 0 ||
                (merged.length === 1 &&
                  merged[0][0] === 0 &&
                  merged[0][1] === entry.body_length)))
          ),
        });
      }
      state.progress.receipts_version++;
      state.progress.unread_count = state.entries.filter(
        (e) => state.receipts.get(e.key)?.unread !== false,
      ).length;
      const response = { ...state.progress };
      await state.delayObservation;
      return route.fulfill({ json: response });
    }
    if (path.endsWith("/mark")) {
      const command = request.postDataJSON();
      if (command.receipts_version !== state.progress.receipts_version)
        return route.fulfill({
          status: 409,
          json: { detail: "reading_conflict" },
        });
      state.marks.push(command.action);
      for (const entry of state.entries.filter(
        (e) => command.action === "snapshot_read" || e.key === command.key,
      )) {
        const prior = state.receipts.get(entry.key) ?? fresh(),
          read = command.action !== "entry_unread";
        state.receipts.set(entry.key, {
          ...fresh(),
          generation: prior.generation + (read ? 0 : 1),
          explicit_read: read,
          header_read: read,
          unread: !read,
        });
      }
      state.progress.receipts_version++;
      return respond(route);
    }
    if (path.endsWith("/claim")) {
      const command = request.postDataJSON();
      if (command.position_revision !== state.progress.position_revision)
        return route.fulfill({
          status: 409,
          json: { detail: "position_conflict" },
        });
      state.claims++;
      state.writer = command.session_id;
      state.progress.writer_epoch++;
      state.progress.position_revision++;
      return respond(route);
    }
    if (path.endsWith("/position")) {
      const command = request.postDataJSON();
      if (
        state.writer !== command.session_id ||
        state.progress.writer_epoch !== command.writer_epoch
      )
        return route.fulfill({
          status: 409,
          json: { detail: "position_conflict" },
        });
      state.saves++;
      state.progress.anchor = command.anchor as ReadingAnchor;
      state.progress.position_revision++;
      const response = { ...state.progress };
      await state.delayPosition;
      return route.fulfill({ json: response });
    }
    return route.fulfill({ status: 404, json: { detail: "missing" } });
  });
  await page.clock.install({ time: new Date(now) });
  await page.clock.pauseAt(new Date(now).getTime() + 100);
  await page.goto("/#flow/42/conversation");
  await expect(page.locator('[data-window-list="会话活动"]')).toBeVisible();
  await expect(page.getByLabel("个人阅读进度")).toContainText("条未读");
}

test("only visible content counts after dwell; jumping over history leaves holes", async ({
  page,
}) => {
  const body = "长段落😀".repeat(900),
    state = model(
      [
        item("long", body),
        ...Array.from({ length: 24 }, (_, i) => item(`skipped-${i}`)),
      ],
      new Map([["long", body]]),
    );
  await serve(page, state);
  await page.evaluate(() => {
    document.documentElement.dataset.focus = "off";
    document.documentElement.dataset.visibility = "hidden";
  });
  await page.clock.runFor(1000);
  expect(state.observations).toHaveLength(0);
  await page.evaluate(() => {
    document.documentElement.dataset.focus = "on";
    document.documentElement.dataset.visibility = "visible";
  });
  await page.clock.runFor(500);
  expect(state.observations).toHaveLength(0);
  await page.clock.runFor(400);
  await expect.poll(() => state.observations.length).toBeGreaterThan(0);
  const spans = state.observations
    .filter((e) => e.key === "long")
    .flatMap((e) => e.ranges);
  expect(spans.length).toBeGreaterThan(0);
  expect(Math.max(...spans.map((e) => e[1]))).toBeLessThan(2000);
  expect(state.receipts.get("long")?.unread).toBe(true);
  await page.locator(".detail-body").evaluate((root) => {
    root.scrollTop = root.scrollHeight;
  });
  await page.clock.runFor(1000);
  expect(state.observations.some((e) => e.key === "skipped-5")).toBe(false);
  expect(state.claims).toBe(0);
  expect(state.marks).toEqual([]);
});

test("explicit unread stays unread in the current viewport and snapshot marking is confirmed", async ({
  page,
}, testInfo) => {
  const state = model([item("short")]);
  await serve(page, state);
  await page.screenshot({ path: testInfo.outputPath("default.png") });
  await page.clock.runFor(900);
  await expect(
    page.getByRole("button", { name: "已读 · 标为未读" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "已读 · 标为未读" }).click();
  await expect(
    page.getByRole("button", { name: "未读 · 标为已读" }),
  ).toBeVisible();
  const sent = state.observations.length;
  await page.clock.runFor(1500);
  expect(state.observations).toHaveLength(sent);
  expect(state.receipts.get("short")?.generation).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("unread.png") });
  await page.getByRole("button", { name: "标记当前快照为已读" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "包含未加载的历史和回复",
  );
  expect(state.marks).toEqual(["entry_unread"]);
  await page.screenshot({ path: testInfo.outputPath("confirmation.png") });
  await page.getByRole("button", { name: "确认标记", exact: true }).click();
  await expect
    .poll(() => state.marks)
    .toEqual(["entry_unread", "snapshot_read"]);
  expect(state.claims).toBe(0);
});

test("cross-device resume restores a body segment and fences the old writer", async ({
  page,
  browser,
}, testInfo) => {
  const body = "跨设备正文😀\n".repeat(700),
    state = model([item("long", body)], new Map([["long", body]]));
  await serve(page, state);
  await page.getByRole("button", { name: "下一段", exact: true }).click();
  await page.getByRole("button", { name: "在此继续阅读", exact: true }).click();
  await expect.poll(() => state.saves).toBeGreaterThan(0);
  expect(state.progress.anchor?.body_offset).toBeGreaterThan(0);
  const offset = state.progress.anchor!.body_offset,
    claims = state.claims;
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
  });
  const phone = await context.newPage();
  await serve(phone, state);
  await expect(
    phone.getByText("已恢复阅读位置", { exact: false }),
  ).toBeVisible();
  await expect(
    phone.getByText(`字符 ${offset + 1}–`, { exact: false }),
  ).toBeVisible();
  expect(state.claims).toBe(claims);
  await phone.screenshot({ path: testInfo.outputPath("phone-resume.png") });
  await phone
    .getByRole("button", { name: "在此继续阅读", exact: true })
    .click();
  await expect.poll(() => state.claims).toBe(claims + 1);
  await page.bringToFront();
  // Keyboard dispatch completes before advancing the controlled dwell/debounce clock.
  await page
    .getByRole("button", { name: "下一段", exact: true })
    .press("ArrowUp");
  await page.clock.runFor(500);
  await expect(
    page.getByText("另一设备已更新位置。", { exact: false }),
  ).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath("position-conflict.png") });
  await page
    .getByRole("button", { name: "下一段", exact: true })
    .press("ArrowUp");
  await page.clock.runFor(500);
  expect(state.claims).toBe(claims + 1);
  await context.close();
});

for (const background of [false, true]) {
  test(`delayed position save ${background ? "discards background navigation" : "preserves the final body segment"}`, async ({
    page,
  }) => {
    const body = "跨设备正文😀\n".repeat(700),
      state = model([item("long", body)], new Map([["long", body]]));
    let release!: () => void;
    state.delayPosition = new Promise<void>((resolve) => {
      release = resolve;
    });
    await serve(page, state);
    await page
      .getByRole("button", { name: "在此继续阅读", exact: true })
      .click();
    await expect.poll(() => state.saves).toBe(1);
    const firstOffset = state.progress.anchor!.body_offset;
    // The user finishes another segment before the first save response arrives.
    await page.getByRole("button", { name: "下一段", exact: true }).click();
    await page.clock.runFor(500);
    expect(state.saves).toBe(1);
    if (background)
      await page.evaluate(() => {
        document.documentElement.dataset.focus = "off";
        window.dispatchEvent(new Event("blur"));
      });
    const response = page.waitForResponse("**/reading/position");
    release();
    await response;
    if (background) {
      await page.clock.runFor(500);
      expect(state.saves).toBe(1);
      expect(state.progress.anchor!.body_offset).toBe(firstOffset);
    } else {
      await expect.poll(() => state.saves).toBe(2);
      expect(state.progress.anchor!.body_offset).toBeGreaterThan(firstOffset);
    }
    expect(state.claims).toBe(1);
  });
}

test("failed observations retry the exact ranges without moving the reading position", async ({
  page,
}, testInfo) => {
  const state = model([item("short")]);
  state.failObservation = true;
  await serve(page, state);
  await page.clock.runFor(900);
  await expect(page.getByRole("alert")).toContainText("阅读状态未同步");
  expect(state.observations).toHaveLength(0);
  await page.screenshot({ path: testInfo.outputPath("unsynced.png") });
  await page.getByRole("button", { name: "重试同步", exact: true }).click();
  await expect.poll(() => state.progress.unread_count).toBe(0);
  expect(state.observations.flatMap((e) => e.ranges)).toEqual([[0, 6]]);
  expect(state.claims).toBe(0);
});

test("resuming a tool result opens its collapsed group without claiming the position", async ({
  page,
}) => {
  const entry = {
    ...item("result", "工具结果正文"),
    source: "worker",
    kind: "tool_result",
  };
  const state = model([entry]);
  state.progress.anchor = {
    key: entry.key,
    version: 1,
    read_revision: 1,
    snapshot: 1,
    parent_key: null,
    part: "body",
    body_offset: 0,
    block_fraction: 0,
    occurred_at: now,
    layout_version: 1,
  };
  await serve(page, state);
  await expect(page.getByText("工具结果正文", { exact: true })).toBeVisible();
  expect(state.claims).toBe(0);
});

test("Markdown syntax and astral text count once, while collapsed tools stay unread", async ({
  page,
}) => {
  const markdown =
    "# 标题😀\n\n**强调**与[链接](https://example.test)\n\n```js\nconst x = '😀';\n```\n";
  const state = model([
    item("markdown", markdown),
    { ...item("continuation", "同作者的后续消息"), title: "" },
    {
      ...item("tool", "不可见的工具结果"),
      source: "worker",
      kind: "tool_result",
    },
  ]);
  await serve(page, state);
  await page.clock.runFor(900);
  await expect.poll(() => state.receipts.get("markdown")?.unread).toBe(false);
  await expect
    .poll(() => state.receipts.get("continuation")?.unread)
    .toBe(false);
  expect(
    state.observations.filter((e) => e.key === "tool").flatMap((e) => e.ranges),
  ).toEqual([]);
  expect(state.progress.unread_count).toBe(1);
});

test("late observation responses cannot undo a newer explicit unread", async ({
  page,
}) => {
  const state = model([item("short")]);
  let release!: () => void;
  state.delayObservation = new Promise<void>((resolve) => {
    release = resolve;
  });
  await serve(page, state);
  await page.clock.runFor(900);
  await expect.poll(() => state.observations.length).toBeGreaterThan(0);
  // Read the latest receipt version while the successful observation reply is delayed.
  await page.getByRole("button", { name: "标记当前快照为已读" }).click();
  await page.getByRole("button", { name: "确认标记", exact: true }).click();
  await page.getByRole("button", { name: "已读 · 标为未读" }).click();
  const response = page.waitForResponse("**/reading/observations");
  release();
  await response;
  await page.clock.runFor(1000);
  await expect(
    page.getByRole("button", { name: "未读 · 标为已读" }),
  ).toBeVisible();
  await expect(page.getByLabel("个人阅读进度")).toContainText("1 条未读");
  expect(state.receipts.get("short")?.generation).toBe(1);
});

for (const [reason, notice] of [
  ["edited", "原位置的内容已更新"],
  ["deleted", "原位置的内容已删除"],
  ["missing", "原位置已失效"],
  ["incompatible", "原位置的布局已变化"],
] as const) {
  test(`resume explains ${reason} without rewriting the saved position`, async ({
    page,
  }, testInfo) => {
    const state = model([item("short")]);
    state.resumeReason = reason;
    const original: ReadingAnchor = {
      key: "short",
      version: 1,
      read_revision: 1,
      snapshot: 1,
      parent_key: null,
      part: "body",
      body_offset: 3,
      block_fraction: 0.5,
      occurred_at: now,
      layout_version: 1,
    };
    state.progress.anchor = original;
    await serve(page, state);
    await expect(page.getByText(notice, { exact: false })).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(`fallback-${reason}.png`),
    });
    expect(state.progress.anchor).toEqual(original);
    expect(state.claims).toBe(0);
    await page.getByRole("button", { name: "保存此处为新位置" }).click();
    await expect.poll(() => state.saves).toBe(1);
  });
}
