import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:5174",
    viewport: { width: 1440, height: 900 },
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "npm run dev -- --config e2e/vite.config.ts --port 5174 --strictPort",
    url: "http://127.0.0.1:5174",
  },
});
