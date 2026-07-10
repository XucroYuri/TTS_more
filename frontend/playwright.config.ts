import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.TTS_MORE_E2E_BASE_URL ?? "http://127.0.0.1:5173";
const serverUrl = new URL(baseURL);
const serverPort = serverUrl.port || (serverUrl.protocol === "https:" ? "443" : "80");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 180 * 60 * 1000,
  expect: {
    timeout: 30_000
  },
  reporter: [
    ["line"],
    ["junit", { outputFile: "test-results/playwright-junit.xml" }]
  ],
  outputDir: "test-results/artifacts",
  use: {
    ...devices["Desktop Chrome"],
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  },
  webServer: {
    command: `pnpm dev -- --host 127.0.0.1 --port ${serverPort}`,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120_000,
    env: {
      ...process.env,
      TTS_MORE_API_TARGET: process.env.TTS_MORE_API_TARGET ?? "http://127.0.0.1:8000"
    }
  }
});
