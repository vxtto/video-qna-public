import { defineConfig } from "@playwright/test";

// Points at whatever's already running (docker-compose's api service, or a
// dev server) - this suite doesn't manage the stack's lifecycle itself, see
// e2e/README.md for how CI/local runs bring it up and seed the fixture
// video first.
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:8000";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  // Short on purpose (see repo conversation this was scoped from): these
  // two tests are the only browser-automation layer in the suite, kept
  // deliberately small so they stay fast and non-flaky.
  timeout: 15_000,
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
