import assert from "node:assert/strict"
import test from "node:test"

import { COST_NOTE, formatMoney } from "../src/cost.ts"

test("money keeps Azure's currency and the precision asked for", () => {
  assert.equal(formatMoney(12.5, "EUR", "en"), "€12.50")
  // A monthly estimate in whole units; per-request prices keep a third digit.
  assert.equal(formatMoney(300.0008, "USD", "en", 0), "$300")
  assert.equal(formatMoney(0.035, "USD", "en", 3), "$0.035")
  assert.equal(formatMoney(0.03, "USD", "en", 3), "$0.03")
})

test("an unknown currency code still shows the amount", () => {
  assert.equal(formatMoney(12.5, "credits", "en"), "12.50 credits")
})

test("every note has a message", () => {
  assert.deepEqual(Object.keys(COST_NOTE).sort(), ["forbidden", "no_token", "unavailable", "unsupported"])
})
