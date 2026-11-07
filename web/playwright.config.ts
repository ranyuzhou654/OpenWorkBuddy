import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  use: { baseURL: "http://127.0.0.1:8766", viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure", screenshot: "only-on-failure" },
  reporter: [["list"], ["json", { outputFile: "test-results/results.json" }]],
  webServer: { command: "uv run --project .. python ../tests/fixtures/e2e_server.py",
    url: "http://127.0.0.1:8766/__test__/ready", reuseExistingServer: false },
});
