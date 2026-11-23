import assert from "node:assert/strict"
import test from "node:test"

import { DEFAULT_BREAKER } from "../../src/aigateway/pool-breaker.ts"
import { poolChanges } from "../../src/aigateway/pool-changes.ts"
import { narrate, rescue, route, ruleMatches, simulate } from "../../src/aigateway/routing-sim.ts"

const member = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })
const input = (members, overrides = {}) => ({
  members,
  breaker: DEFAULT_BREAKER,
  retries: 2,
  failing: new Set(),
  kind: "throttle",
  ...overrides,
})
const states = (result) => Object.fromEntries(result.members.map((m) => [m.backend_id, [m.state, Math.round(m.share), m.failing]]))
const keys = (lines) => lines.map((line) => line.key)

test("traffic goes to the lowest priority and splits by weight", () => {
  const { shares, priority } = route([member("a", 0, 3), member("b", 0, 1), member("c", 1)])
  assert.equal(priority, 0)
  assert.deepEqual(Object.fromEntries(shares), { a: 75, b: 25 })
})

test("a matched failure moves the request on and excludes the member", () => {
  const failover = input([member("a"), member("b", 1)], { failing: new Set(["a"]) })
  assert.equal(rescue(failover, "a"), "fallback")
  assert.deepEqual(states(simulate(failover, "tripped")), { a: ["tripped", 0, false], b: ["serving", 100, false] })
  assert.equal(simulate(failover, "failing").outcome, "ok")
  assert.ok(keys(narrate(failover, "failing", (id) => id)).includes("simExcludeRule"))
  const balanced = input([member("a"), member("b")], { failing: new Set(["a"]) })
  assert.equal(rescue(balanced, "a"), "retry")
})

test("a failure the rule doesn't catch reaches the client and excludes nothing", () => {
  const breaker = { ...DEFAULT_BREAKER, status_code_ranges: [{ minimum: 500, maximum: 599 }] }
  const pool = input([member("a"), member("b", 1)], { breaker, failing: new Set(["a"]) })
  assert.equal(ruleMatches(breaker, "throttle"), false)
  assert.equal(ruleMatches(breaker, "outage"), true)
  assert.equal(rescue(pool, "a"), null)
  assert.equal(simulate(pool, "failing").outcome, "degraded")
  assert.deepEqual(states(simulate(pool, "tripped")), { a: ["serving", 100, true], b: ["standby", 0, false] })
  assert.ok(keys(narrate(pool, "failing", (id) => id)).includes("simNoRule"))
})

test("without retries a matched failure has nowhere to go", () => {
  const pool = input([member("a"), member("b", 1)], { retries: 0, failing: new Set(["a"]) })
  assert.equal(rescue(pool, "a"), null)
  assert.equal(simulate(pool, "failing").outcome, "degraded")
  assert.ok(keys(narrate(pool, "failing", (id) => id)).includes("simNoRetries"))
  // It is still excluded, so later requests go to the standby.
  assert.equal(simulate(pool, "tripped").outcome, "ok")
})

test("an exclusion of 0 seconds skips the member for the failed request only", () => {
  const pool = input([member("a"), member("b", 1)], { breaker: { ...DEFAULT_BREAKER, exclude_seconds: 0 }, failing: new Set(["a"]) })
  assert.deepEqual(states(simulate(pool, "tripped")), { a: ["serving", 100, true], b: ["standby", 0, false] })
  assert.equal(simulate(pool, "tripped").outcome, "ok")
})

test("publishing lists what changes", () => {
  const before = { members: [member("a"), member("b", 1)], title: "Pool", retries: 2, circuit_breaker: DEFAULT_BREAKER }
  const after = { ...before, members: [member("a"), member("c", 1)], retries: 3 }
  assert.deepEqual(poolChanges(before, after).map((change) => change.kind), ["added", "removed", "settings"])
  assert.deepEqual(poolChanges(null, before)[0], { kind: "created", mode: "failover", count: 2 })
})
