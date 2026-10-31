// Browser end-to-end tests: the built UI (frontend/, npm run build) served by
// scripts/demo_server.py, which runs the real FastAPI app on the backend tests' in-memory
// Azure, APIM service and LiteLLM gateway. Nothing here reaches Azure.
import path from "node:path"
import { defineConfig, devices } from "@playwright/test"

const root = path.resolve(__dirname, "../..")
const python = process.env.E2E_PYTHON
  ?? path.join(root, "backend", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
const port = Number(process.env.E2E_PORT ?? 8010)
export const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: "./tests",
  // One demo server, one in-memory state: each test resets it to a stage, so one at a time.
  workers: 1,
  fullyParallel: false,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    baseURL,
    locale: "en-US",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } } }],
  webServer: {
    command: `"${python}" scripts/demo_server.py --port ${port}`,
    cwd: root,
    url: `${baseURL}/api/config`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    stdout: "pipe",
    stderr: "pipe",
  },
})
