// The LiteLLM gateway itself: deploying one with the capacity sizer, its panel (cost, resize,
// switch), and the Monitor and Gateway metrics tabs of a pool in use.
import { dialog, expect, gatewayButton, openAt, POOL, stage, test } from "./fixtures"

test("a new gateway is sized, checked, deployed and opened", async ({ page }) => {
  await stage(page, "sign-in")
  await page.goto("/")
  await page.getByRole("button", { name: "Sign in with browser" }).click()
  await page.getByRole("radio", { name: /^LiteLLM gateway/ }).check()
  await page.getByRole("button", { name: "Deploy a gateway" }).click()

  // The sizer recommends a platform and shows how big the gateway has to be.
  await expect(page.getByText("Size the gateway for your traffic")).toBeVisible()
  await page.getByRole("spinbutton", { name: /^Peak requests a minute/ }).fill("600")
  await expect(page.getByText(/replicas · PostgreSQL/).first()).toBeVisible()
  await page.getByRole("button", { name: "Deploy on Container Apps" }).click()

  // The region is checked before anything can be deployed.
  await page.getByRole("textbox", { name: /^Name/ }).fill("gw-e2e")
  await expect(page.getByText(/has the capacity and quota for this gateway/)).toBeVisible()
  await page.getByRole("button", { name: "Deploy", exact: true }).click()

  // The demo's deployment finishes after a few polls; then the app opens on the new gateway.
  await expect(page.getByText(/Deploying gw-e2e|Deployment in progress/).first()).toBeVisible()
  await expect(page.getByRole("navigation").getByRole("button", { name: "gw-e2e" })).toBeVisible({ timeout: 45_000 })
})

test("the gateway panel shows cost, resizes and switches", async ({ page }) => {
  await openAt(page, "pool", "litellm")
  await gatewayButton(page, "litellm").click()

  const panel = dialog(page, "Gateway ai-gateway-contoso")
  await expect(panel.getByText("This month so far")).toBeVisible()
  await expect(panel.getByText("List price").first()).toBeVisible()

  // Resizing lists every change and needs a confirmation before it applies.
  await panel.getByRole("button", { name: "Resize for new traffic" }).click()
  const resize = dialog(page, "Resize the gateway for new traffic")
  await resize.getByRole("spinbutton", { name: /^Peak requests a minute/ }).fill("6000")
  await expect(resize.getByRole("columnheader", { name: "Now" })).toBeVisible({ timeout: 20_000 })
  await expect(resize.getByRole("cell", { name: "Replicas" })).toBeVisible()
  const apply = resize.getByRole("button", { name: "Resize", exact: true })
  await expect(apply).toBeDisabled()
  await resize.getByRole("checkbox", { name: /^I understand this may affect production traffic/ }).check()
  await apply.click()
  // The plan is read again before it applies: as slow as the first read on a cold server.
  await expect(resize.getByText(/^Changed: /)).toBeVisible({ timeout: 20_000 })
  await resize.getByRole("button", { name: "Close" }).last().click()
  await expect(resize).toBeHidden()

  // Back on the panel: switching opens the gateway picker on its own.
  await panel.getByRole("button", { name: "Switch or create" }).click()
  await expect(page.getByRole("button", { name: "Deploy a gateway" })).toBeVisible()
  await expect(page.getByRole("listitem").filter({ hasText: "litellm-platform" })).toBeVisible()
})

test("a pool in use shows its metrics and the gateway's load", async ({ page }) => {
  await openAt(page, "pool", "litellm")
  await page.getByRole("listitem").filter({ hasText: POOL }).first().click()
  await page.getByRole("tab", { name: "Metrics", exact: true }).click()
  const metrics = page.getByRole("tabpanel", { name: "Metrics", exact: true })
  await expect(metrics.getByText(/Requests/).first()).toBeVisible({ timeout: 20_000 })
  await expect(metrics.locator("svg").first()).toBeVisible()

  await page.getByRole("tab", { name: "Gateway metrics" }).click()
  await expect(page.getByText("Database connections").first()).toBeVisible({ timeout: 20_000 })
  await expect(page.locator("main svg").first()).toBeVisible()
})
