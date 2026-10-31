// APIM: deployments become a pool with an API bound to it, the policy the binding wrote, and a
// test call through the service's gateway; a pool in use, its metrics, and the service panel.
import { dialog, expect, gatewayButton, openAt, POOL, test } from "./fixtures"

test("deployments become a pool bound to a new API that answers a test call", async ({ page }) => {
  await openAt(page, "ready", "apim", "deployments")
  const astra = page.locator("section").filter({ has: page.getByRole("checkbox", { name: "Select all gpt-6-astra" }) })
  await astra.getByRole("button", { name: "Create pool from this model" }).click()

  // A new pool gets an API bound to it by default.
  const editor = dialog(page, "Add pool")
  await expect(editor.getByRole("checkbox", { name: /^Also create an API and bind it to this pool/ })).toBeChecked()
  await expect(editor.getByRole("button", { name: "Failover" })).toHaveAttribute("aria-pressed", "true")
  await editor.getByRole("button", { name: "Publish to APIM" }).click()
  await dialog(page, "Review and publish").getByRole("button", { name: "Publish to APIM" }).click()

  const guide = page.getByRole("tabpanel", { name: /^Test call/ })
  await expect(guide.getByText("Bound APIs", { exact: true })).toBeVisible()
  await expect(guide.getByRole("button", { name: "Copy https://apim-contoso.azure-api.net/gpt-6-astra" })).toBeVisible()

  // The binding is a marked block in the API's policy.
  await guide.getByRole("button", { name: "View policy" }).click()
  const policy = page.getByRole("dialog").last()
  await expect(policy.getByText(/apim-backend-pool:inbound:begin/).first()).toBeVisible()
  await policy.getByRole("button", { name: "Close" }).first().click()
  await expect(policy).toBeHidden()

  // The service's identity gets access, then a subscription key, then the call.
  await guide.getByRole("button", { name: /^Grant all/ }).click()
  await expect(guide.getByRole("button", { name: /^Grant all/ })).toBeHidden()
  await guide.getByRole("button", { name: "Create a test key for gpt-6-astra" }).click()
  await expect(guide.getByText(/Key ready/).first()).toBeVisible()
  await guide.getByRole("button", { name: "Send request" }).click()
  await expect(guide.getByText(/^The call succeeded/)).toBeVisible()
})

test("an APIM pool in use shows its metrics, cost and switch", async ({ page }) => {
  await openAt(page, "pool", "apim")
  await page.getByRole("listitem").filter({ hasText: POOL }).first().click()
  await page.getByRole("tab", { name: "Metrics", exact: true }).click()
  await expect(page.getByRole("tabpanel", { name: "Metrics", exact: true }).locator("svg").first()).toBeVisible({ timeout: 20_000 })

  await page.getByRole("tab", { name: "Gateway metrics" }).click()
  await expect(page.locator("main svg").first()).toBeVisible({ timeout: 20_000 })

  await gatewayButton(page, "apim").click()
  const panel = dialog(page, "APIM service apim-contoso")
  await expect(panel.getByText("This month so far")).toBeVisible()
  await panel.getByRole("button", { name: "Switch or create" }).click()
  await expect(page.getByRole("listitem").filter({ hasText: "apim-contoso" }).first()).toBeVisible()
})
