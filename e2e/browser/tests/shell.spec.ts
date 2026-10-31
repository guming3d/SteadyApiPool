// The shell around every page: the appearance toggle and the language switch keep the app
// working, and the choice survives a reload.
import { expect, openAt, test } from "./fixtures"

test("appearance and language switch without breaking the page", async ({ page }) => {
  await openAt(page, "pool", "litellm")
  const html = page.locator("html")

  // System, then light, then dark.
  await page.getByRole("button", { name: /^Appearance: System/ }).click()
  await expect(html).toHaveAttribute("data-mode", "light")
  await page.getByRole("button", { name: /^Appearance: Light/ }).click()
  await expect(html).toHaveAttribute("data-mode", "dark")
  await expect(page.getByRole("heading", { name: "Pools", level: 1 })).toBeVisible()

  await page.getByRole("button", { name: "Language" }).click()
  await expect(page.getByRole("tab", { name: "后端池" })).toBeVisible()
  await expect(page.getByRole("listitem").filter({ hasText: "gpt-5-6-luna" }).first()).toBeVisible()
  await page.reload()
  await expect(page.getByRole("tab", { name: "后端池" })).toBeVisible()
  await expect(html).toHaveAttribute("data-mode", "dark")

  await page.getByRole("button", { name: "Language" }).click()
  await expect(page.getByRole("tab", { name: "Pools" })).toBeVisible()
})
