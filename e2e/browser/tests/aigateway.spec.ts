// Azure AI Gateway: deployments become a pool (a model alias with the app's failure rule) that
// answers a test call through the gateway's shared test key; a pool in use and the gateway panel.
import { dialog, expect, gatewayButton, openAt, POOL, test } from "./fixtures"

test("deployments become an alias with a failure rule that answers a test call", async ({ page }) => {
  await openAt(page, "ready", "aigateway", "deployments")
  const astra = page.locator("section").filter({ has: page.getByRole("checkbox", { name: "Select all gpt-6-astra" }) })
  await astra.getByRole("button", { name: "Create pool from this model" }).click()

  const editor = dialog(page, "Add pool")
  await expect(editor.getByRole("button", { name: "Failover" })).toHaveAttribute("aria-pressed", "true")
  await editor.getByRole("button", { name: "Publish to the gateway" }).click()
  await dialog(page, "Review and publish").getByRole("button", { name: "Publish to the gateway" }).click()

  const guide = page.getByRole("tabpanel", { name: /^Test call/ })
  await expect(guide).toBeVisible()
  // The gateway's identity gets Foundry User on the accounts, then the gateway-wide test key.
  await guide.getByRole("button", { name: /^Grant all/ }).click()
  await expect(guide.getByText(/The gateway can call all 2 account/)).toBeVisible()
  await expect(guide.getByText(/opens every pool on this gateway, not only this one/).first()).toBeVisible()
  await guide.getByRole("button", { name: "Create a test key" }).click()
  await guide.getByRole("button", { name: "Send request" }).click()
  await expect(guide.getByText(/^The call succeeded/)).toBeVisible()
})

test("an AI Gateway pool in use shows its failure rule, metrics and the gateway's cost", async ({ page }) => {
  await openAt(page, "pool", "aigateway")
  await page.getByRole("listitem").filter({ hasText: POOL }).first().click()

  await page.getByRole("tab", { name: "Settings" }).click()
  const settings = page.getByRole("tabpanel", { name: "Settings" })
  await expect(settings.getByRole("spinbutton", { name: /^Exclude a failed member for/ })).toHaveValue("60")
  await expect(settings.getByRole("spinbutton", { name: /^Retry limit/ })).toHaveValue("2")

  await page.getByRole("tab", { name: "Metrics", exact: true }).click()
  await expect(page.getByText(/telemetry exporter/i)).toHaveCount(0)

  await gatewayButton(page, "aigateway").click()
  await expect(page.getByText("No charge listed").first()).toBeVisible()
})
