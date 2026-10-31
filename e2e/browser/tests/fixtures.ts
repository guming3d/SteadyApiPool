// What every journey starts from: the demo server reset to a stage, and a browser that speaks
// English and has seen the product tour (it would cover the page on a first run).
import { expect, test as base, type Page } from "@playwright/test"

export type Product = "litellm" | "apim"
export type Stage = "sign-in" | "ready" | "pool"

export const GATEWAY = { litellm: "ai-gateway-contoso", apim: "apim-contoso" } as const
// The pool the "pool" stage has in use (scripts/demo_server.py POOL).
export const POOL = "gpt-5-6-luna"

type Options = {
  /** Mark setup as seen this session, so the setup wizard doesn't open by itself. */
  setupSeen: boolean
}

export const test = base.extend<Options>({
  setupSeen: [true, { option: true }],
  page: async ({ page, setupSeen }, use) => {
    // Runs before every page load, reloads too: defaults only, so a choice made in a test stays.
    await page.addInitScript((seen) => {
      const defaults: [Storage, string, string][] = [
        [localStorage, "ai-gateway-pool:locale", "en"],
        [localStorage, "ai-gateway-pool:tour", "1"],
      ]
      if (seen) defaults.push([sessionStorage, "ai-gateway-pool:setup-seen", "1"])
      for (const [storage, key, value] of defaults) {
        if (storage.getItem(key) === null) storage.setItem(key, value)
      }
    }, setupSeen)
    await use(page)
  },
})

export { expect }

/** Resets the demo server's fakes to ``stage`` on a ``product`` gateway. */
export async function stage(page: Page, name: Stage, product: Product = "litellm") {
  const response = await page.request.post(`/demo/stage/${name}?product=${product}`)
  expect(response.status(), `demo stage ${name}`).toBe(204)
}

/** Opens the app at ``tab`` once the demo is at ``name`` on ``product``. */
export async function openAt(page: Page, name: Stage, product: Product, tab = "pools") {
  await stage(page, name, product)
  await page.goto(`/?tab=${tab}`)
  await expect(gatewayButton(page, product)).toBeVisible()
}

/** The chosen gateway's name in the top bar, which opens the gateway panel. */
export function gatewayButton(page: Page, product: Product) {
  return page.getByRole("navigation").getByRole("button", { name: GATEWAY[product] })
}

/** The open dialog with this heading (dialogs can be nested; this is the innermost match). */
export function dialog(page: Page, title: string | RegExp) {
  return page.getByRole("dialog").filter({ has: page.getByRole("heading", { name: title }) }).last()
}
