// A LiteLLM pool's life: from discovered deployments to a published pool, a test call, an edit
// and its removal.
import { dialog, expect, openAt, test } from "./fixtures"

test("deployments become a failover pool that answers a test call", async ({ page }) => {
  await openAt(page, "ready", "litellm", "deployments")

  // Discovered Foundry deployments, grouped by model.
  await expect(page.getByRole("heading", { name: "Deployments", level: 1 })).toBeVisible()
  // Each model is a section of its own.
  const astra = page.locator("section").filter({ has: page.getByRole("checkbox", { name: "Select all gpt-6-astra" }) })
  await expect(page.getByRole("checkbox", { name: "foundry-eastus2 gpt-6-astra" })).toBeVisible()
  await expect(page.getByRole("checkbox", { name: "foundry-swedencentral gpt-6-astra" })).toBeVisible()
  await astra.getByRole("button", { name: "Create pool from this model" }).click()

  // The editor starts as failover: the first deployment primary, the other a standby.
  const editor = dialog(page, "Add pool")
  await expect(editor.getByRole("textbox", { name: /^Pool ID/ })).toHaveValue("pool-gpt-6-astra")
  await expect(editor.getByRole("button", { name: "Failover" })).toHaveAttribute("aria-pressed", "true")
  await expect(editor.getByRole("radio", { name: /^Primary: gpt-6-astra · foundry-eastus2/ })).toBeChecked()
  await expect(editor.getByRole("radio", { name: /^Make primary: gpt-6-astra · foundry-swedencentral/ })).not.toBeChecked()
  await editor.getByRole("button", { name: "Publish to the gateway" }).click()

  // Publishing shows what changes first.
  const review = dialog(page, "Review and publish")
  await expect(review.getByText("Create pool (Failover, 2 members)")).toBeVisible()
  await review.getByRole("button", { name: "Publish to the gateway" }).click()

  // The pool is listed and opens on its call guide.
  await expect(page.getByRole("listitem").filter({ hasText: "pool-gpt-6-astra" })).toBeVisible()
  await expect(page.getByText("Deployed", { exact: true })).toBeVisible()
  const guide = page.getByRole("tabpanel", { name: "Test call" })
  await expect(guide).toBeVisible()

  // Before calling: the gateway's identity gets access to the accounts, then a test key.
  await guide.getByRole("button", { name: /^Grant all/ }).click()
  await expect(guide.getByText(/The gateway can call all 2 account/)).toBeVisible()
  await guide.getByRole("button", { name: "Create a test key" }).click()
  await expect(guide.getByText("Key ready; test calls use it.", { exact: false })).toBeVisible()
  await guide.getByRole("button", { name: "Send request" }).click()
  await expect(guide.getByText(/The call succeeded: answered by pool-foundry-eastus2-gpt-6-astra/)).toBeVisible()
})

test("a pool's settings are edited, published, and the pool removed", async ({ page }) => {
  await openAt(page, "pool", "litellm")
  await page.getByRole("listitem").filter({ hasText: "gpt-5-6-luna" }).first().click()

  await page.getByRole("tab", { name: "Settings" }).click()
  const settings = page.getByRole("tabpanel", { name: "Settings" })
  await settings.getByRole("spinbutton", { name: "Cooldown (s)" }).fill("120")
  await settings.getByRole("spinbutton", { name: /^Retries/ }).fill("2")
  await page.getByRole("button", { name: "Publish to the gateway" }).first().click()

  const review = dialog(page, "Review and publish")
  await expect(review.getByRole("listitem").first()).toBeVisible()
  await review.getByRole("button", { name: "Publish to the gateway" }).click()
  await expect(review).toBeHidden()

  // Read back from the gateway after a reload.
  await page.reload()
  await page.getByRole("listitem").filter({ hasText: "gpt-5-6-luna" }).first().click()
  await page.getByRole("tab", { name: "Settings" }).click()
  await expect(page.getByRole("tabpanel", { name: "Settings" }).getByRole("spinbutton", { name: "Cooldown (s)" })).toHaveValue("120")

  // Removing goes through a plan of what goes with it.
  await page.getByRole("button", { name: "Delete pool" }).click()
  const removal = dialog(page, "Delete pool gpt-5-6-luna")
  await expect(removal.getByText("Delete the pools")).toBeVisible()
  await expect(removal.getByText("gpt-5-6-luna").first()).toBeVisible()
  await removal.getByRole("button", { name: "Delete", exact: true }).click()
  await expect(page.getByText("No backend pools")).toBeVisible()
})
