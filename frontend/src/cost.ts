// The service panel's cost section; see components/service-panel.tsx and pool_manager/cost.py.
import type { CostNote } from "./types"

/** Money in the reader's language; amounts keep the currency Azure gave them. */
export function formatMoney(amount: number, currency: string, locale: string, maxDigits = 2): string {
  try {
    return new Intl.NumberFormat(locale === "zh" ? "zh-CN" : "en-US", {
      style: "currency",
      currency,
      minimumFractionDigits: Math.min(2, maxDigits),
      maximumFractionDigits: maxDigits,
    }).format(amount)
  } catch {
    // Not an ISO currency code.
    return `${amount.toFixed(Math.min(2, maxDigits))} ${currency}`
  }
}

/** The message shown in place of this month's spend. */
export const COST_NOTE: Record<CostNote, "costNoToken" | "costForbidden" | "costUnsupported" | "costUnavailable"> = {
  no_token: "costNoToken",
  forbidden: "costForbidden",
  unsupported: "costUnsupported",
  unavailable: "costUnavailable",
}
