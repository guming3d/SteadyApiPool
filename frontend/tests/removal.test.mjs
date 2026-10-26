import assert from "node:assert/strict"
import test from "node:test"

import { confirmPlan, nameMatches, planIsEmpty } from "../src/removal.ts"

const alias = (name, pool_id = "p1") => ({ name, pool_id })
const key = (alias, id = `hash-${alias}`) => ({ id, alias, pool_id: "p1" })
const plan = (extra = {}) => ({
  pools: [], delete_aliases: [], unbind_aliases: [], delete_keys: [], kept_pools: [], blocked: [], ...extra,
})

test("the confirmation lists exactly the plan shown", () => {
  const value = plan({
    pools: ["p1"],
    delete_aliases: [alias("gpt-4o")],
    unbind_aliases: [alias("by-hand")],
    delete_keys: [key("pool-test-p1"), { id: "hash-only", alias: null, pool_id: "p1" }],
  })
  assert.deepEqual(confirmPlan(value, '"7"'), {
    pools: ["p1"],
    delete_aliases: ["gpt-4o"],
    unbind_aliases: ["by-hand"],
    delete_keys: ["pool-test-p1", "hash-only"],
    etag: '"7"',
  })
  assert.equal("etag" in confirmPlan(value), false)
})

test("a plan with nothing to remove is empty, whatever it keeps", () => {
  assert.equal(planIsEmpty(plan({ kept_pools: ["hand-made"] })), true)
  assert.equal(planIsEmpty(plan({ delete_keys: [key("pool-test-old")] })), false)
  assert.equal(planIsEmpty(plan({ unbind_aliases: [alias("left-over")] })), false)
})

test("the typed name ignores case and surrounding spaces", () => {
  assert.equal(nameMatches("  GW-Prod ", "gw-prod"), true)
  assert.equal(nameMatches("gw", "gw-prod"), false)
})
