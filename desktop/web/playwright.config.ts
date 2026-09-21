import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const repositoryRoot = path.resolve(import.meta.dirname, "..", "..");
const defaultPython = process.platform === "win32"
  ? path.join(repositoryRoot, "desktop", ".venv", "Scripts", "python.exe")
  : path.join(repositoryRoot, "desktop", ".venv", "bin", "python");
const python = process.env.MARKETLISTENER_PYTHON || defaultPython;
const browser = process.env.CI
  ? { ...devices["Desktop Chrome"] }
  : { ...devices["Desktop Chrome"], channel: "chrome" as const };

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  use: { ...browser, baseURL: "http://127.0.0.1:8767" },
  webServer: {
    command: `"${python}" -m market_monitor.cli serve --data-root data_control --host 127.0.0.1 --port 8767 --quiet`,
    cwd: repositoryRoot,
    url: "http://127.0.0.1:8767/api/health",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
