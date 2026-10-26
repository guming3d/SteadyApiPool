import assert from "node:assert/strict"
import test from "node:test"

import { confirmPlan, nameMatches, planIsEmpty } from "../src/removal.ts"

const api = (id, pool_id = "p1") => ({ id, display_name: id, path: id, pool_id })
const plan = (extra = {}) => ({
  pools: [], delete_apis: [], unbind_apis: [], delete_members: [], kept_pools: [], blocked: [], ...extra,
})

test("the confirmation lists exactly the plan shown", () => {
  const value = plan({ pools: ["p1"], delete_apis: [api("p1")], unbind_apis: [api("legacy")], delete_members: ["m-a"] })
  assert.deepEqual(confirmPlan(value, '"7"'), { pools: ["p1"], delete_apis: ["p1"], unbind_apis: ["legacy"], etag: '"7"' })
  assert.deepEqual(confirmPlan(value), { pools: ["p1"], delete_apis: ["p1"], unbind_apis: ["legacy"] })
})

test("a plan with nothing to remove is empty, whatever it keeps", () => {
  assert.equal(planIsEmpty(plan({ kept_pools: ["hand-made"] })), true)
  assert.equal(planIsEmpty(plan({ delete_members: ["orphan"] })), false)
  assert.equal(planIsEmpty(plan({ delete_apis: [api("left-over", null)] })), false)
})

test("the typed name ignores case and surrounding spaces", () => {
  assert.equal(nameMatches("  APIM-Prod ", "apim-prod"), true)
  assert.equal(nameMatches("apim", "apim-prod"), false)
})
