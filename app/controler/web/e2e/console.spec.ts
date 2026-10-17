import { expect, test, type Page } from "@playwright/test";

declare global {
  interface Window {
    browserErrors: string[];
  }
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.browserErrors = [];
    // ResizeObserver delivery errors are window events, not always Playwright pageerrors.
    window.addEventListener("error", (event) => {
      window.browserErrors.push(event.message);
    });
  });
});

test.afterEach(async ({ page }) => {
  // Include delivery errors from the last layout, not just errors preceding the final action.
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }),
  );
  expect(await page.evaluate(() => window.browserErrors)).toEqual([]);
  expect(await page.pageErrors()).toEqual([]);
});

async function expectBounded(page: Page) {
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
}

test("mobile navigation wraps focus around visible controls after collapsing a group", async ({
  page,
}) => {
  // Closed details descendants can retain layout rectangles despite being unfocusable.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#flow/42/overview");
  await page.getByRole("button", { name: "切换任务" }).click();
  const dialog = page.getByRole("dialog", { name: "Flow 导航" });
  const first = dialog.getByRole("button", { name: "返回看板" });
  const last = dialog.locator("summary").filter({ hasText: "已完成" });
  await last.click();
  await expect(
    dialog.getByRole("button", { name: /增加服务运行指标/ }),
  ).toBeHidden();
  await first.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(last).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(first).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "切换任务" })).toBeFocused();
});

test("virtual lists retain reading state and keyboard order across unmounted rows", async ({
  page,
}) => {
  // A user can expand long text, scroll it out of the DOM, and return to the same segment.
  await page.goto("/e2e/harness.html");
  const root = page.getByRole("region", { name: "长列表" });
  const first = root
    .locator("details")
    .filter({ has: page.locator("summary", { hasText: /^记录 0$/ }) });
  await first.locator("summary").click();
  await first.getByRole("button", { name: "下一段" }).click();
  await expect(first).toContainText("字符 2049–4096");
  await page.getByRole("button", { name: "移出列表焦点" }).click();
  await root.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(
    root.locator("summary").filter({ hasText: /^记录 999$/ }),
  ).toBeVisible();
  await expect(first).toHaveCount(0);
  await expectBounded(page);
  await root.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(first).toHaveAttribute("open", "");
  await expect(first).toContainText("字符 2049–4096");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(first).toContainText("字符 2049–4096");

  // Tab at a mounted window's edge must reach the next logical row, not skip unseen records.
  const rows = root.locator(
    "[data-window-list='回归列表'] > [data-window-row]",
  );
  const edge = rows.last();
  const index = Number(await edge.getAttribute("data-window-row"));
  await edge.locator("summary").focus();
  await page.keyboard.press("Tab");
  await expect(
    root.locator(`[data-window-row="${index + 1}"] summary`),
  ).toBeFocused();
  await expectBounded(page);
});

test("returning from a virtual card restores every board column and the selected card focus", async ({
  page,
}) => {
  await page.goto("/e2e/harness.html?mode=board");
  const root = page.getByRole("region", { name: "执行中任务列表" });
  const count = Number(
    await root.locator("[data-window-count]").getAttribute("data-window-count"),
  );
  expect(count).toBeGreaterThan(60);
  await root.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(
    root.locator(`[data-window-row="${count - 1}"] button`),
  ).toBeVisible();
  await expectBounded(page);
  await page.locator(".column-scroll").evaluateAll((columns) => {
    columns.forEach((column, index) => {
      column.scrollTop = 600 + index * 120;
    });
  });
  const visibleCard = () =>
    root.locator(".card").evaluateAll((cards) => {
      const bounds = cards[0]
        ?.closest(".column-scroll")
        ?.getBoundingClientRect();
      return (
        bounds &&
        cards
          .find((card) => {
            const rect = card.getBoundingClientRect();
            return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
          })
          ?.getAttribute("data-flow")
      );
    });
  // Wait for the new virtual window after scrolling, rather than inspecting the old DOM.
  await expect.poll(visibleCard).toBeTruthy();
  const id = await visibleCard();
  const tops = await page
    .locator(".column-scroll")
    .evaluateAll((columns) => columns.map((column) => column.scrollTop));
  const card = page.locator(`.card[data-flow="${id}"]`);
  await card.click();
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "长看板任务",
  );
  await page.getByRole("button", { name: "返回看板", exact: true }).click();
  await expect(card).toBeFocused();
  expect(
    await page
      .locator(".column-scroll")
      .evaluateAll((columns) => columns.map((column) => column.scrollTop)),
  ).toEqual(tops);
  await expectBounded(page);
});

test("evicted conversation history is readable again without losing expansion or exceeding the cache budget", async ({
  page,
}) => {
  // More than 240 records forces real hook eviction; fast deterministic I/O replaces only the reader.
  await page.goto("/#flow/42/conversation");
  const root = page.locator(".detail-body");
  const list = root.locator('[data-window-list="会话活动"]');
  const first = root.locator('[data-entry="history-0"]');
  await first.locator(".tool summary").click();
  await page.getByRole("heading", { level: 1 }).focus();
  let count = Number(await list.getAttribute("data-window-count"));
  while (count < 300) {
    await root.evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await page.waitForFunction(
      (before) =>
        Number(
          document
            .querySelector('[data-window-list="会话活动"]')
            ?.getAttribute("data-window-count"),
        ) > before,
      count,
    );
    count = Number(await list.getAttribute("data-window-count"));
    expect(
      Number(
        await page
          .locator(".conversation-stream")
          .getAttribute("data-cached-rows"),
      ),
    ).toBeLessThanOrEqual(240);
    await expectBounded(page);
  }
  await expect(root.locator('[data-entry="history-299"]')).toBeVisible();
  await expect(first).toHaveCount(0);
  await root.evaluate((element) => {
    element.scrollTop = 0;
  });
  await expect(first).toContainText("历史交互 0");
  await expect(first.locator(".tool")).toHaveAttribute("open", "");
  await root.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(root.locator('[data-entry="history-299"]')).toBeVisible();
  await root.getByRole("button", { name: /点击刷新/ }).click();
  await expect(list).toHaveAttribute("data-window-count", "302");
  await expect(root.getByText("本次新增交互", { exact: true })).toBeVisible();
  await expectBounded(page);
});
