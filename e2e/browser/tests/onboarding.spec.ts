// First run: signed out, sign in, choose the kind of gateway, choose one, and reach the app.
import { expect, gatewayButton, stage, test, type Product } from "./fixtures"

test.use({ setupSeen: false })

const choices: { product: Product; kind: RegExp; gateway: string }[] = [
  { product: "litellm", kind: /^LiteLLM gateway/, gateway: "ai-gateway-contoso" },
  { product: "apim", kind: /^Azure API Management/, gateway: "apim-contoso" },
]

for (const { product, kind, gateway } of choices) {
  test(`a first run signs in and chooses the ${product} gateway`, async ({ page }) => {
    await stage(page, "sign-in")
    await page.goto("/")

    // Nothing works until setup is done, so it takes the whole page.
    await expect(page.getByRole("heading", { name: "Get ready to manage pools" })).toBeVisible()
    await expect(page.getByRole("button", { name: "2 AI gateway" })).toBeDisabled()
    await page.getByRole("button", { name: "Sign in with browser" }).click()
    await expect(page.getByText("alex@contoso.com")).toBeVisible()

    await page.getByRole("radio", { name: kind }).check()
    const listed = page.getByRole("listitem").filter({ hasText: gateway })
    await expect(listed).toBeVisible()
    await listed.getByRole("button", { name: /^Use this/ }).click()

    // The app opens on the chosen gateway.
    await expect(gatewayButton(page, product)).toBeVisible({ timeout: 20_000 })
    await expect(page.getByRole("heading", { name: "Pools", level: 1 })).toBeVisible()
    await expect(page.getByRole("tab", { name: "Pools" })).toHaveAttribute("aria-selected", "true")
  })
}
