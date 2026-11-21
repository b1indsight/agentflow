import { expect, test, type Page, type Route } from "@playwright/test";
import type { CodingModelSettings, FlowDetail, ModelCatalog } from "../types";

const defaults: CodingModelSettings = {
  codex_model: null,
  amp_model: null,
  codex_effort: null,
  amp_effort: null,
  revision: 0,
};
const artifactRoot =
  process.env.PLAYWRIGHT_ARTIFACT_DIR ?? "../../../.amp/in/artifacts";
const model = (id: string, efforts: string[], displayName = id) => ({
  id,
  displayName,
  efforts,
});
const levels = ["low", "medium", "high", "xhigh", "max"];
const catalog: ModelCatalog = {
  codex: {
    status: "ok",
    models: [
      model("gpt-6.1-sol", [...levels, "ultra"], "GPT-6.1 Sol"),
      model("gpt-6-astra", [...levels, "ultra"]),
      model("gpt-6-luna", levels),
      model("gpt-5.6-luna", levels),
      model("gpt-6.1-terra", levels),
    ],
  },
  amp: {
    status: "ok",
    models: [
      model("anthropic/claude-opus-5-5", levels, "Claude Opus 5.5"),
      model("openai/gpt-6.1-sol", levels),
      model("deepseek/deepseek-v4.1-flash", ["none", "high", "max"]),
      model("xai/grok-4.5", []),
      model("xai/grok-build-0.1", []),
      model("minimax/MiniMax-M3", ["none", "high"]),
      model("anthropic/claude-sonnet-4-6", ["none", "high"]),
      model("google-vertex/gemini-3.1-pro-preview", ["low", "medium", "high"]),
    ],
  },
};
const flow: FlowDetail = {
  id: 42,
  history_only: false,
  continued_from_flow_id: null,
  installation_id: 71,
  repo: "owner/agentflow",
  issue_number: null,
  issue_url: null,
  title: "模型设置导航回归",
  pr_number: null,
  pr_url: null,
  lifecycle: "running",
  phase: "running",
  action: "正在验证",
  worker_id: "worker-1",
  worker_historical: false,
  revision_cycles: 0,
  branch: "flow/42",
  created_at: "2026-11-14T12:00:00Z",
  updated_at: "2026-11-14T12:00:00Z",
  current_attempt_id: 1,
  attempt_started_at: "2026-10-17T12:00:00Z",
  archive_reason: null,
  archive_summary: null,
  archived_at: null,
  state_version: 1,
  reason: "正在执行",
  next_condition: "等待结果",
  sources: [],
  task_content: "保留原有会话读取与看板状态。",
};

/** Mutable server state and request gates make races deterministic without real services. */
async function server(page: Page, initial = defaults) {
  const state = {
    saved: { ...initial },
    puts: [] as Record<string, unknown>[],
    reads: [] as string[],
    get: null as ((route: Route) => Promise<void>) | null,
    put: null as ((route: Route) => Promise<void>) | null,
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/settings/model-catalog") {
      expect(request.method()).toBe("GET");
      state.reads.push(url.pathname);
      return route.fulfill({ json: catalog });
    }
    if (url.pathname === "/api/settings") {
      if (request.method() === "PUT") {
        const body = request.postDataJSON();
        state.puts.push(body);
        if (state.put) return state.put(route);
        state.saved = {
          codex_model: body.codex_model,
          amp_model: body.amp_model,
          codex_effort: body.codex_effort,
          amp_effort: body.amp_effort,
          revision: state.saved.revision + 1,
        };
      } else {
        state.reads.push(url.pathname);
        if (state.get) return state.get(route);
      }
      return route.fulfill({ json: state.saved });
    }
    state.reads.push(url.pathname);
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
          counts: { queued: 0, running: 1, review: 0, blocked: 0, archived: 0 },
        },
      });
    if (url.pathname === "/api/flows/42") return route.fulfill({ json: flow });
    if (url.pathname.endsWith("/inference-notices"))
      return route.fulfill({ json: { ...pageOf([]), snapshot: 1 } });
    if (url.pathname.endsWith("/activity"))
      return route.fulfill({
        json: {
          ...pageOf([]),
          snapshot: 1,
          total: 0,
          sources: [],
          changes: {},
        },
      });
    return route.fulfill({ status: 500, json: { detail: "unexpected_api" } });
  });
  return state;
}

const codex = (page: Page) =>
  page.getByRole("combobox", { name: "Codex 模型" });
const amp = (page: Page) => page.getByRole("combobox", { name: "Amp 模型" });
const codexEffort = (page: Page) =>
  page.getByRole("combobox", { name: "Codex 推理强度" });
const ampEffort = (page: Page) =>
  page.getByRole("combobox", { name: "Amp 推理强度" });
const save = (page: Page) =>
  page.getByRole("button", { name: "保存设置", exact: true });

test("an empty discovery is distinct from a failed provider and keeps saved defaults", async ({
  page,
}) => {
  const state = await server(page, {
    ...defaults,
    amp_model: "retired/model",
    amp_effort: "prior-level",
    codex_effort: "medium",
  });
  await page.route("**/api/settings/model-catalog", (route) =>
    route.fulfill({
      json: {
        codex: { status: "ok", models: [] },
        amp: { status: "unavailable", models: [] },
      },
    }),
  );
  await page.goto("/console/#settings");
  await expect(page.getByText("Codex 目录未返回模型")).toBeVisible();
  await expect(page.getByText("Amp 模型目录不可用")).toBeVisible();
  await expect(codex(page).locator("option")).toHaveCount(1);
  await expect(amp(page)).toHaveValue("retired/model");
  await expect(ampEffort(page)).toHaveValue("prior-level");
  await expect(save(page)).toBeDisabled();
  expect(state.puts).toHaveLength(0);
});

for (const width of [1440, 390]) {
  test(`dynamic catalog refresh discovers models without changing draft or revision at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    const state = await server(page, {
      ...defaults,
      amp_model: "old/saved",
      revision: 4,
    });
    await page.goto("/console/#settings");
    await amp(page).selectOption("anthropic/claude-opus-5-5");
    await ampEffort(page).selectOption("max");
    await expect(codex(page).locator('option[value="new-model"]')).toHaveCount(
      0,
    );
    // A worker comes online while another goes offline. Neither discovery nor its retry is a settings reread.
    const reads = state.reads.length;
    const refresh = page.getByRole("button", { name: "刷新模型目录" });
    await page.route("**/api/settings/model-catalog", (route) =>
      route.fulfill({
        json: {
          codex: {
            status: "partial",
            models: [
              ...catalog.codex.models,
              model("new-model", ["high"], "新模型"),
            ],
          },
          amp: { status: "unavailable", models: [] },
        },
      }),
    );
    await refresh.click();
    await expect(page.getByText("Codex 模型目录部分可用")).toBeVisible();
    await expect(page.getByText("Amp 模型目录不可用")).toBeVisible();
    await expect(amp(page)).toHaveValue("anthropic/claude-opus-5-5");
    await expect(amp(page).locator('option[value="old/saved"]')).toHaveCount(1);
    await expect(ampEffort(page)).toHaveValue("max");
    await codex(page).selectOption({ label: "新模型" });
    await codexEffort(page).selectOption("high");
    await page.screenshot({
      path: `${artifactRoot}/catalog-partial-${width}.png`,
    });
    await page.route("**/api/settings/model-catalog", (route) =>
      route.abort("failed"),
    );
    await refresh.click();
    await expect(page.getByText(/模型目录读取失败/)).toBeVisible();
    await expect(codexEffort(page)).toHaveValue("high");
    await expect(ampEffort(page)).toHaveValue("max");
    await page.screenshot({
      path: `${artifactRoot}/catalog-failure-${width}.png`,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    expect(state.reads).toHaveLength(reads);
    expect(state.puts).toHaveLength(0);
    await save(page).click();
    await expect(page.getByRole("status")).toContainText("设置已保存");
    expect(state.puts).toEqual([
      {
        codex_model: "new-model",
        codex_effort: "high",
        amp_model: "anthropic/claude-opus-5-5",
        amp_effort: "max",
        expected_revision: 4,
      },
    ]);
  });

  test(`settings four-field save, reload, defaults and accessible layout at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 390 ? 1200 : 1000 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const state = await server(page);
    await page.goto("/console/#settings");
    await expect(
      page.getByRole("heading", { name: "Code agent 模型" }),
    ).toBeVisible();
    await expect(codex(page)).toHaveValue("");
    await expect(amp(page)).toHaveValue("");
    await expect(codexEffort(page)).toHaveValue("medium");
    await expect(codexEffort(page).locator("option:checked")).toHaveText("mid");
    await expect(ampEffort(page)).toHaveValue("");
    await expect(ampEffort(page)).toBeDisabled();
    await expect(page.getByRole("textbox")).toHaveCount(0);
    await expect(
      amp(page).locator(
        'option:is([value="low"], [value="medium"], [value="high"], [value="ultra"])',
      ),
    ).toHaveCount(0);
    await expect(
      page.getByText(/预设快照|当前设置已读取|模型默认/),
    ).toHaveCount(0);
    await expect(page.getByText(/^(Codex|Amp) 推理强度$/)).toHaveCount(0);
    await expect(
      page.getByRole("link", { name: "设置", exact: true }),
    ).toHaveText("");
    await expect(save(page)).toBeEnabled();
    await page.screenshot({
      path: `${artifactRoot}/code-agent-settings-compact-default-${width}.png`,
      animations: "disabled",
    });
    await save(page).click();
    await expect(save(page)).toBeDisabled();
    expect(state.puts.at(-1)).toEqual({
      codex_model: null,
      amp_model: null,
      codex_effort: "medium",
      amp_effort: null,
      expected_revision: 0,
    });

    await codex(page).selectOption("gpt-6.1-sol");
    await amp(page).selectOption("anthropic/claude-opus-5-5");
    await expect(codexEffort(page)).toHaveValue("medium");
    await expect(ampEffort(page)).toHaveValue("medium");
    await expect(page.getByText("未保存修改", { exact: true })).toBeVisible();
    expect(state.puts).toHaveLength(1);
    await codex(page).focus();
    await page.keyboard.press("Tab");
    await expect(codexEffort(page)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(amp(page)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(ampEffort(page)).toBeFocused();
    await page.screenshot({
      path: `${artifactRoot}/code-agent-settings-compact-selected-${width}.png`,
      animations: "disabled",
    });
    await codexEffort(page).selectOption("ultra");
    await ampEffort(page).selectOption("max");
    await save(page).click();
    await expect(page.getByRole("status")).toContainText("设置已保存");
    expect(state.puts.at(-1)).toEqual({
      codex_model: "gpt-6.1-sol",
      amp_model: "anthropic/claude-opus-5-5",
      codex_effort: "ultra",
      amp_effort: "max",
      expected_revision: 1,
    });
    await page.screenshot({
      path: `${artifactRoot}/code-agent-settings-compact-saved-${width}.png`,
      animations: "disabled",
    });

    await page.reload();
    await expect(codex(page)).toHaveValue("gpt-6.1-sol");
    await expect(amp(page)).toHaveValue("anthropic/claude-opus-5-5");
    await expect(codexEffort(page)).toHaveValue("ultra");
    await expect(ampEffort(page)).toHaveValue("max");
    await codex(page).selectOption("");
    await amp(page).selectOption("");
    await codexEffort(page).selectOption("medium");
    await save(page).click();
    await expect(save(page)).toBeDisabled();
    expect(state.puts.at(-1)).toEqual({
      codex_model: null,
      amp_model: null,
      codex_effort: "medium",
      amp_effort: null,
      expected_revision: 2,
    });
    await page.reload();
    await expect(codex(page)).toHaveValue("");
    await expect(amp(page)).toHaveValue("");
    await expect(codexEffort(page)).toHaveValue("medium");
    await expect(ampEffort(page)).toHaveValue("");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await expect(codex(page)).toHaveCSS("border-radius", "0px");
    expect(errors).toEqual([]);
  });

  test(`settings read failure, validation and conflict preserve drafts at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: width === 390 ? 1200 : 1000 });
    const state = await server(page);
    state.get = (route) =>
      route.fulfill({ status: 503, json: { detail: "database_unavailable" } });
    await page.goto("/console/#settings");
    await expect(page.getByRole("alert")).toContainText("设置读取失败");
    await expect(codex(page)).toBeDisabled();
    await expect(amp(page)).toBeDisabled();
    await expect(codexEffort(page)).toBeDisabled();
    await expect(ampEffort(page)).toBeDisabled();
    await expect(save(page)).toBeDisabled();
    await page.screenshot({
      path: `${artifactRoot}/code-agent-settings-compact-read-error-${width}.png`,
      animations: "disabled",
    });
    state.get = null;
    await page
      .getByRole("button", { name: "重新读取设置", exact: true })
      .click();
    await expect(codex(page)).toBeEnabled();
    await codex(page).selectOption("gpt-6.1-sol");
    await amp(page).selectOption("openai/gpt-6.1-sol");
    await codexEffort(page).selectOption("ultra");
    await ampEffort(page).selectOption("max");
    state.put = (route) =>
      route.fulfill({ status: 422, json: { detail: "invalid_model" } });
    await save(page).click();
    await expect(page.getByRole("alert")).toContainText("校验失败");
    await expect(codex(page)).toHaveValue("gpt-6.1-sol");
    await expect(amp(page)).toHaveValue("openai/gpt-6.1-sol");
    await expect(codexEffort(page)).toHaveValue("ultra");
    await expect(ampEffort(page)).toHaveValue("max");
    state.put = (route) =>
      route.fulfill({ status: 409, json: { detail: "settings_conflict" } });
    await codex(page).selectOption("gpt-6-astra");
    await save(page).click();
    await expect(page.getByRole("alert")).toContainText("设置冲突");
    await expect(save(page)).toBeDisabled();
    await expect(codex(page)).toHaveValue("gpt-6-astra");
    await expect(codexEffort(page)).toHaveValue("ultra");
    await expect(ampEffort(page)).toHaveValue("max");
    await page.screenshot({
      path: `${artifactRoot}/code-agent-settings-compact-conflict-${width}.png`,
      animations: "disabled",
    });
    state.saved = {
      codex_model: "another-owner-tab",
      amp_model: "another-provider/new-model",
      codex_effort: "medium",
      amp_effort: "low",
      revision: 7,
    };
    state.get = (route) =>
      route.fulfill({ status: 503, json: { detail: "database_unavailable" } });
    await page
      .getByRole("button", { name: "重新读取设置", exact: true })
      .click();
    await expect(page.getByText(/设置读取失败/)).toBeVisible();
    await expect(codex(page)).toHaveValue("gpt-6-astra");
    await expect(save(page)).toBeDisabled();
    state.get = null;
    await page
      .getByRole("button", { name: "重新读取设置", exact: true })
      .click();
    await expect(
      page.getByRole("region", { name: "服务器当前设置" }),
    ).toContainText("another-owner-tab");
    await expect(codex(page)).toHaveValue("gpt-6-astra");
    await expect(codexEffort(page)).toHaveValue("ultra");
    await expect(ampEffort(page)).toHaveValue("max");
    state.put = null;
    await save(page).click();
    await expect(page.getByRole("status")).toContainText("设置已保存");
    expect(state.puts.at(-1)).toEqual({
      codex_model: "gpt-6-astra",
      amp_model: "openai/gpt-6.1-sol",
      codex_effort: "ultra",
      amp_effort: "max",
      expected_revision: 7,
    });
  });
}

for (const outcome of [
  "network",
  "503",
  "malformed",
  "effort-mismatch",
] as const) {
  test(`settings unknown ${outcome} write is verified without another PUT or draft replacement`, async ({
    page,
  }) => {
    const state = await server(page);
    await page.goto("/console/#settings");
    await expect(codex(page)).toBeEnabled();
    await codex(page).selectOption("gpt-6.1-sol");
    await amp(page).selectOption("anthropic/claude-opus-5-5");
    await codexEffort(page).selectOption("ultra");
    await ampEffort(page).selectOption("max");
    state.put = async (route) => {
      state.saved = {
        codex_model: "gpt-6.1-sol",
        amp_model: "anthropic/claude-opus-5-5",
        codex_effort: "ultra",
        amp_effort: "max",
        revision: state.saved.revision + 1,
      };
      if (outcome === "network") return route.abort("failed");
      return route.fulfill({
        status: outcome === "503" ? 503 : 200,
        json:
          outcome === "503"
            ? { detail: "database_unavailable" }
            : outcome === "effort-mismatch"
              ? { ...state.saved, amp_effort: "low" }
              : { revision: 1 },
      });
    };
    await save(page).click();
    await expect(page.getByRole("alert")).toContainText("保存结果未知");
    await expect(save(page)).toBeDisabled();
    if (outcome === "network") {
      for (const width of [1440, 390]) {
        await page.setViewportSize({
          width,
          height: width === 390 ? 1200 : 1000,
        });
        await expect(codex(page)).toHaveValue("gpt-6.1-sol");
        await expect(amp(page)).toHaveValue("anthropic/claude-opus-5-5");
        await expect(codexEffort(page)).toHaveValue("ultra");
        await expect(ampEffort(page)).toHaveValue("max");
        await expect(
          page.getByRole("button", { name: "回读核对", exact: true }),
        ).toBeEnabled();
        await page.screenshot({
          path: `${artifactRoot}/code-agent-settings-compact-unknown-${width}.png`,
          animations: "disabled",
        });
      }
    }
    // Navigating away must not clear the uncertain-write lock and allow a blind resend.
    await page.getByRole("button", { name: "返回看板" }).click();
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("已回读核对");
    await expect(codex(page)).toHaveValue("gpt-6.1-sol");
    await expect(codexEffort(page)).toHaveValue("ultra");
    await expect(ampEffort(page)).toHaveValue("max");
    expect(state.puts).toHaveLength(1);

    // An effort-only edit must be dirty and share the same uncertain-write protection.
    await codexEffort(page).selectOption("high");
    await save(page).click();
    await expect(page.getByRole("alert")).toContainText("保存结果未知");
    await expect(
      page.getByRole("button", { name: "使用服务器设置", exact: true }),
    ).toHaveCount(0);
    await codexEffort(page).selectOption("medium");
    await ampEffort(page).selectOption("xhigh");
    state.get = (route) =>
      route.fulfill({ status: 503, json: { detail: "database_unavailable" } });
    await page.getByRole("button", { name: "回读核对", exact: true }).click();
    await expect(page.getByText(/设置读取失败/)).toBeVisible();
    await expect(save(page)).toBeDisabled();
    // Matching models and a newer revision cannot confirm a write whose effort differs.
    state.saved.codex_effort = "high";
    state.saved.amp_effort = "low";
    state.get = null;
    await page.getByRole("button", { name: "回读核对", exact: true }).click();
    await expect(page.getByRole("status")).toContainText("与上次提交不同");
    await expect(
      page.getByRole("region", { name: "服务器当前设置" }),
    ).toBeVisible();
    await expect(codexEffort(page)).toHaveValue("medium");
    await expect(ampEffort(page)).toHaveValue("xhigh");
    expect(state.puts).toHaveLength(2);
    await page
      .getByRole("button", { name: "使用服务器设置", exact: true })
      .click();
    await expect(codex(page)).toHaveValue("gpt-6.1-sol");
    await expect(codexEffort(page)).toHaveValue("high");
    await expect(ampEffort(page)).toHaveValue("low");
    await expect(save(page)).toBeDisabled();
  });
}

test("settings isolates late reads, saving and route changes from the current draft", async ({
  page,
}) => {
  const state = await server(page);
  await page.goto("/console/#settings");
  await expect(codex(page)).toBeEnabled();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  state.get = async (route) => {
    await gate;
    await route.fulfill({
      json: { ...defaults, codex_model: "late-old-model", codex_effort: "low" },
    });
  };
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(page.getByRole("status")).toContainText("正在读取设置");
  await codexEffort(page).selectOption("ultra");
  release();
  await expect(page.getByRole("status")).not.toContainText("正在读取设置");
  await expect(codex(page)).toHaveValue("");
  await expect(codexEffort(page)).toHaveValue("ultra");
  let finish!: () => void;
  const pending = new Promise<void>((resolve) => {
    finish = resolve;
  });
  state.put = async (route) => {
    await pending;
    await route.fulfill({
      json: {
        ...defaults,
        codex_effort: "ultra",
        revision: 1,
      },
    });
  };
  await save(page).dblclick();
  await expect(page.getByRole("status")).toContainText("正在保存");
  await expect(codex(page)).toBeDisabled();
  await expect(codexEffort(page)).toBeDisabled();
  await expect(ampEffort(page)).toBeDisabled();
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  expect(state.puts).toHaveLength(1);
  finish();
  await expect(page.getByRole("status")).toContainText("设置已保存");
  await expect(codex(page)).toHaveValue("");
  await expect(codexEffort(page)).toHaveValue("ultra");

  let releaseOld!: () => void;
  const oldGate = new Promise<void>((resolve) => {
    releaseOld = resolve;
  });
  let oldReadDone!: () => void;
  const oldDone = new Promise<void>((resolve) => {
    oldReadDone = resolve;
  });
  state.get = async (route) => {
    await oldGate;
    await route.fulfill({ json: defaults });
    oldReadDone();
  };
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(page.getByRole("status")).toContainText("正在读取设置");
  await page.getByRole("button", { name: "返回看板" }).click();
  state.get = null;
  state.saved = {
    ...defaults,
    codex_model: "new-route-model",
    amp_model: "provider/new-route-model",
    codex_effort: "future-codex-effort",
    amp_effort: "future-amp-effort",
    revision: 2,
  };
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await expect(codex(page)).toHaveValue("new-route-model");
  releaseOld();
  await oldDone;
  await expect(codex(page)).toHaveValue("new-route-model");
  await expect(codexEffort(page)).toHaveValue("future-codex-effort");
  await expect(ampEffort(page)).toHaveValue("future-amp-effort");
});

test("settings malformed reads and auth refusals stay actionable without changing the draft", async ({
  page,
}) => {
  const state = await server(page);
  state.get = (route) =>
    route.fulfill({ status: 401, json: { detail: "unauthorized" } });
  await page.goto("/console/#settings");
  await expect(page.getByRole("alert")).toContainText("登录已失效");
  await expect(save(page)).toBeDisabled();
  for (const malformed of [
    { ...defaults, amp_model: { id: "provider/model" } },
    { ...defaults, codex_effort: 3 },
    { ...defaults, codex_effort: undefined },
    { ...defaults, amp_effort: undefined },
  ]) {
    state.get = (route) => route.fulfill({ json: malformed });
    await page
      .getByRole("button", { name: "重新读取设置", exact: true })
      .click();
    await expect(page.getByRole("alert")).toContainText("设置响应不完整");
    await expect(save(page)).toBeDisabled();
  }
  state.get = null;
  await page.getByRole("button", { name: "重新读取设置", exact: true }).click();
  await expect(codex(page)).toBeEnabled();
  await codex(page).selectOption("gpt-6.1-sol");
  state.put = (route) =>
    route.fulfill({ status: 403, json: { detail: "forbidden" } });
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("没有操作权限");
  await expect(codex(page)).toHaveValue("gpt-6.1-sol");
  expect(state.puts).toHaveLength(1);
});

test("settings preserves noncatalog current identifiers through selection, rereads and exact saves", async ({
  page,
}) => {
  // An offline worker can leave saved IDs absent from discovery; never silently save them as null.
  const state = await server(page, {
    ...defaults,
    codex_model: "codex-future-model",
    amp_model: "future-provider/Future-Model",
    codex_effort: "future-codex-effort",
    amp_effort: "future-amp-effort",
    revision: 4,
  });
  await page.goto("/console/#settings");
  await expect(codex(page)).toHaveValue("codex-future-model");
  await expect(amp(page)).toHaveValue("future-provider/Future-Model");
  await expect(codex(page).locator("option:checked")).toContainText("目录外");
  await expect(amp(page).locator("option:checked")).toContainText("目录外");
  await expect(codexEffort(page).locator("option:checked")).toContainText(
    "目录外",
  );
  await expect(ampEffort(page).locator("option:checked")).toContainText(
    "目录外",
  );
  await expect(save(page)).toBeDisabled();
  await codex(page).selectOption("gpt-6.1-sol");
  await expect(codexEffort(page)).toHaveValue("medium");
  await save(page).click();
  await expect(page.getByRole("status")).toContainText("设置已保存");
  expect(state.puts.at(-1)).toEqual({
    codex_model: "gpt-6.1-sol",
    amp_model: "future-provider/Future-Model",
    codex_effort: "medium",
    amp_effort: "future-amp-effort",
    expected_revision: 4,
  });
  await amp(page).selectOption("minimax/MiniMax-M3");
  state.saved = {
    ...defaults,
    codex_model: "server-newer-codex",
    amp_model: "server/newer-amp",
    codex_effort: "another-codex-effort",
    amp_effort: "another-amp-effort",
    revision: 6,
  };
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(
    page.getByRole("region", { name: "服务器当前设置" }),
  ).toContainText("server/newer-amp");
  await expect(codex(page)).toHaveValue("gpt-6.1-sol");
  await expect(amp(page)).toHaveValue("minimax/MiniMax-M3");
  await page
    .getByRole("button", { name: "使用服务器设置", exact: true })
    .click();
  await expect(codex(page)).toHaveValue("server-newer-codex");
  await expect(amp(page)).toHaveValue("server/newer-amp");
  await expect(codexEffort(page)).toHaveValue("another-codex-effort");
  await expect(ampEffort(page)).toHaveValue("another-amp-effort");
  await codex(page).selectOption("gpt-6.1-terra");
  await save(page).click();
  await expect(page.getByRole("status")).toContainText("设置已保存");
  expect(state.puts.at(-1)).toEqual({
    codex_model: "gpt-6.1-terra",
    amp_model: "server/newer-amp",
    codex_effort: "medium",
    amp_effort: "another-amp-effort",
    expected_revision: 6,
  });
  await page.reload();
  await expect(amp(page)).toHaveValue("server/newer-amp");
  await expect(ampEffort(page)).toHaveValue("another-amp-effort");
});

test("settings model-dependent efforts preserve compatible selections and reset incompatible ones without saving", async ({
  page,
}) => {
  const state = await server(page);
  await page.goto("/console/#settings");
  await expect(codexEffort(page)).toBeEnabled();
  await expect(codexEffort(page).locator("option")).toHaveText([
    "low",
    "mid",
    "high",
    "xhigh",
    "max",
    "ultra",
  ]);
  await expect(ampEffort(page).locator("option")).toHaveText(["—"]);
  await expect(ampEffort(page)).toBeDisabled();
  await codex(page).selectOption("gpt-6.1-sol");
  await expect(codexEffort(page).locator("option")).toHaveText([
    "low",
    "mid",
    "high",
    "xhigh",
    "max",
    "ultra",
  ]);
  await codexEffort(page).selectOption("ultra");
  await codex(page).selectOption("gpt-6-astra");
  await expect(codexEffort(page)).toHaveValue("ultra");
  await codex(page).selectOption("gpt-6-luna");
  await expect(codexEffort(page)).toHaveValue("medium");
  await expect(codexEffort(page).locator('option[value="ultra"]')).toHaveCount(
    0,
  );
  await codexEffort(page).selectOption("max");
  await codex(page).selectOption("gpt-5.6-luna");
  await expect(codexEffort(page)).toHaveValue("max");
  await expect(codexEffort(page).locator('option[value="ultra"]')).toHaveCount(
    0,
  );
  await amp(page).selectOption("anthropic/claude-opus-5-5");
  await expect(ampEffort(page)).toHaveValue("medium");
  await expect(ampEffort(page).locator("option")).toHaveText([
    "low",
    "mid",
    "high",
    "xhigh",
    "max",
  ]);
  await ampEffort(page).selectOption("max");
  await amp(page).selectOption("deepseek/deepseek-v4.1-flash");
  await expect(ampEffort(page)).toHaveValue("max");
  await amp(page).selectOption("xai/grok-4.5");
  await expect(ampEffort(page)).toHaveValue("");
  await expect(ampEffort(page).locator("option")).toHaveText(["—"]);
  await expect(ampEffort(page)).toBeDisabled();
  await amp(page).selectOption("google-vertex/gemini-3.1-pro-preview");
  await ampEffort(page).selectOption("medium");
  await amp(page).selectOption("anthropic/claude-sonnet-4-6");
  await expect(ampEffort(page)).toHaveValue("");
  await expect(ampEffort(page).locator("option")).toHaveText([
    "选择强度",
    "none",
    "high",
  ]);
  await expect(save(page)).toBeDisabled();
  await ampEffort(page).selectOption("high");
  await expect(save(page)).toBeEnabled();
  await amp(page).selectOption("");
  await expect(ampEffort(page)).toHaveValue("");
  await expect(ampEffort(page).locator("option")).toHaveCount(1);
  await expect(ampEffort(page)).toBeDisabled();
  expect(state.puts).toHaveLength(0);
});

test("settings labels saved noncatalog efforts explicitly even for known or zero-effort models", async ({
  page,
}) => {
  const state = await server(page, {
    ...defaults,
    codex_model: "gpt-6.1-sol",
    amp_model: "xai/grok-4.5",
    codex_effort: "future-codex-effort",
    amp_effort: "future-amp-effort",
  });
  await page.goto("/console/#settings");
  await expect(codexEffort(page)).toHaveValue("future-codex-effort");
  await expect(ampEffort(page)).toHaveValue("future-amp-effort");
  await expect(codexEffort(page).locator("option:checked")).toContainText(
    "目录外",
  );
  await expect(ampEffort(page).locator("option:checked")).toContainText(
    "目录外",
  );
  await page.getByRole("button", { name: "刷新当前视图" }).click();
  await expect(save(page)).toBeDisabled();
  await expect(ampEffort(page)).toHaveValue("future-amp-effort");
  await codexEffort(page).selectOption("high");
  await save(page).click();
  await expect(page.getByRole("status")).toContainText("设置已保存");
  expect(state.puts.at(-1)).toEqual({
    codex_model: "gpt-6.1-sol",
    amp_model: "xai/grok-4.5",
    codex_effort: "high",
    amp_effort: "future-amp-effort",
    expected_revision: 0,
  });
  await amp(page).selectOption("xai/grok-build-0.1");
  await expect(ampEffort(page)).toHaveValue("");
  await expect(ampEffort(page).locator("option")).toHaveCount(1);
});

for (const width of [1440, 390]) {
  test(`settings deep links, refresh and browser navigation preserve query, board and conversation cache at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await server(page);
    await page.goto("/console/?q=repo%3Aagentflow#settings");
    await expect(codex(page)).toBeEnabled();
    const settingsPaths = ["/api/settings", "/api/settings/model-catalog"];
    expect([...new Set(state.reads)].sort()).toEqual(settingsPaths);
    // Development StrictMode may abort a first mount read; count only the explicit refresh delta.
    const initialReads = state.reads.length;
    await page.getByRole("button", { name: "刷新当前视图" }).click();
    await expect
      .poll(() => state.reads.slice(initialReads).sort())
      .toEqual(settingsPaths);
    await page.getByRole("button", { name: "返回看板" }).click();
    await expect(
      page.getByRole("button", { name: /模型设置导航回归/ }),
    ).toBeVisible();
    await expect(
      page.getByRole("combobox", { name: "搜索任务与筛选条件" }),
    ).toHaveValue("repo:agentflow");
    const boardReads = state.reads.filter(
      (path) => path === "/api/flows",
    ).length;
    await page.getByRole("button", { name: /模型设置导航回归/ }).click();
    await expect(page).toHaveURL(/#flow\/42\/conversation$/);
    await expect(
      page.getByRole("tab", { name: "会话", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(
      page.getByRole("button", { name: "已到当前本地快照末尾 · 点击刷新" }),
    ).toBeVisible();
    const activityReads = state.reads.filter((path) =>
      path.endsWith("/activity"),
    ).length;
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await expect(codex(page)).toBeVisible();
    await page.goBack();
    await expect(
      page.getByRole("tab", { name: "会话", exact: true }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(
      page.getByRole("button", { name: "已到当前本地快照末尾 · 点击刷新" }),
    ).toBeVisible();
    expect(
      state.reads.filter((path) => path.endsWith("/activity")),
    ).toHaveLength(activityReads);
    await page.goForward();
    await expect(codex(page)).toBeVisible();
    await page.getByRole("button", { name: "返回看板" }).click();
    await expect(page).toHaveURL(/q=repo%3Aagentflow#board$/);
    expect(state.reads.filter((path) => path === "/api/flows")).toHaveLength(
      boardReads,
    );
    const search = page.getByRole("combobox", { name: "搜索任务与筛选条件" });
    await search.fill("unsubmitted search draft");
    if (width === 390)
      await page
        .locator(".mobile-stages")
        .getByRole("button", { name: /待处理/ })
        .click();
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await page.getByRole("button", { name: "返回看板" }).click();
    await expect(search).toHaveValue("unsubmitted search draft");
    if (width === 390)
      await expect(page.locator(".mobile-stages button.active")).toContainText(
        "待处理",
      );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await search.fill("is:active");
    await search.press("Escape");
    await search.press("Enter");
    await expect(page).toHaveURL(/q=is%3Aactive#board$/);
    await page.goBack();
    await expect(page).toHaveURL(/q=repo%3Aagentflow#settings$/);
    await expect(codex(page)).toBeVisible();
    await page.goForward();
    await expect(search).toHaveValue("is:active");
  });
}

test("returning from settings preserves loaded board pages and column reading position", async ({
  page,
}) => {
  await server(page);
  let reads = 0;
  const items = Array.from({ length: 80 }, (_, index) => ({
    ...flow,
    id: index + 1,
    title: `看板分页任务 ${index + 1}`,
  }));
  await page.route("**/api/flows?**", (route) => {
    reads++;
    const params = new URL(route.request().url()).searchParams;
    const running = params.get("phase") === "running";
    const start = Number(params.get("cursor") ?? 0);
    return route.fulfill({
      json: {
        items: running ? items.slice(start, start + 50) : [],
        counts: { queued: 0, running: 80, review: 0, blocked: 0, archived: 0 },
        next_cursor: running && start === 0 ? "50" : null,
        has_more: running && start === 0,
      },
    });
  });
  await page.goto("/console/#board");
  const column = page.locator('[data-phase="running"]');
  await expect(
    page.getByRole("button", { name: /看板分页任务 1 / }),
  ).toBeVisible();
  await column.evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expect(
    page.getByRole("button", { name: /看板分页任务 50 / }),
  ).toBeVisible();
  await expect(column.locator("[data-window-count]")).toHaveAttribute(
    "data-window-count",
    "80",
  );
  const before = await column.evaluate(async (node) => {
    node.scrollTop = 1200;
    // Let the virtual range measure before bookmarking a real visible card.
    for (let frame = 0; frame < 6; frame++)
      await new Promise(requestAnimationFrame);
    const top = node.getBoundingClientRect().top + node.clientTop;
    const card = [...node.querySelectorAll<HTMLElement>("[data-flow]")].find(
      (row) => {
        const bounds = row.getBoundingClientRect();
        return bounds.top >= top && bounds.bottom <= top + node.clientHeight;
      },
    );
    return card
      ? {
          id: card.dataset.flow,
          offset: card.getBoundingClientRect().top - top,
        }
      : null;
  });
  expect(before).not.toBeNull();
  const pageReads = reads;
  await page.getByRole("link", { name: "设置", exact: true }).click();
  await expect(codex(page)).toBeEnabled();
  await page.getByRole("button", { name: "返回看板" }).click();
  const target = column.locator(`[data-flow="${before!.id}"]`);
  await expect(target).toBeInViewport();
  // Remounting discards estimated heights: preserve the card and its offset, not raw scrollTop.
  await expect
    .poll(() =>
      target.evaluate(async (card, offset) => {
        const root = card.closest<HTMLElement>("[data-scroll-root]")!;
        let delta = 0;
        for (let frame = 0; frame < 6; frame++) {
          await new Promise(requestAnimationFrame);
          delta = Math.max(
            delta,
            Math.abs(
              card.getBoundingClientRect().top -
                root.getBoundingClientRect().top -
                root.clientTop -
                offset,
            ),
          );
        }
        return delta;
      }, before!.offset),
    )
    .toBeLessThan(5);
  expect(reads).toBe(pageReads);
  await expect(column.locator("[data-window-count]")).toHaveAttribute(
    "data-window-count",
    "80",
  );
  // Repeated scrolling follows changing estimated heights as the virtual list measures distant cards.
  await expect
    .poll(async () => {
      await column.evaluate((node) => {
        node.scrollTop = node.scrollHeight;
      });
      return page.getByRole("button", { name: /看板分页任务 80 / }).isVisible();
    })
    .toBe(true);
  expect(reads).toBe(pageReads);
});
