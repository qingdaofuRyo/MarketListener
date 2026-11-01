import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const repositoryRoot = path.resolve(import.meta.dirname, "..", "..");
const defaultPython = process.platform === "win32"
  ? path.join(repositoryRoot, "desktop", ".venv", "Scripts", "python.exe")
  : path.join(repositoryRoot, "desktop", ".venv", "bin", "python");
const python = process.env.MARKETLISTENER_PYTHON || defaultPython;
const testDataRoot = process.env.MARKETLISTENER_E2E_DATA_ROOT || "artifacts/e2e-customer";
const sessionState = path.join(repositoryRoot, "artifacts", "e2e-local-session.json");
const browser = process.env.CI
  ? { ...devices["Desktop Chrome"] }
  : { ...devices["Desktop Chrome"], channel: "chrome" as const };

export default defineConfig({
  globalSetup: "./e2e/global-setup.ts",
  testDir: "./e2e",
  timeout: 30_000,
  workers: 2,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  use: { ...browser, baseURL: "http://127.0.0.1:8767", storageState: sessionState },
  webServer: [{
    command: `"${python}" -m market_monitor.cli serve --data-root "${testDataRoot}" --host 127.0.0.1 --port 8767 --quiet --no-auto-update-on-startup`,
    cwd: repositoryRoot,
    url: "http://127.0.0.1:8767/api/ready",
    reuseExistingServer: false,
    timeout: 30_000,
  }, {
    command: `"${python}" -m market_monitor.web_admin --project-root "${repositoryRoot}" --data-root artifacts/e2e-admin --port 18768 --no-auto-start`,
    cwd: repositoryRoot,
    url: "http://127.0.0.1:18768/api/admin/ready",
    reuseExistingServer: false,
    timeout: 30_000,
  }],
});
