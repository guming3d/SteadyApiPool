import assert from "node:assert/strict"
import test from "node:test"

import { diffLines } from "../src/config-diff.ts"
import { poolChanges } from "../src/pool-changes.ts"
import { narrate, rescue, route, simulate } from "../src/routing-sim.ts"

const member = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })
const input = (members, overrides = {}) => ({
  members,
  breaker: { allowed_fails: 0, cooldown_seconds: 60 },
  retries: 1,
  failing: new Set(),
  kind: "throttle",
  ...overrides,
})
const states = (result) => Object.fromEntries(result.members.map((m) => [m.backend_id, [m.state, Math.round(m.share), m.failing]]))
const keys = (lines) => lines.map((line) => line.key)

test("traffic goes to the lowest order and splits by weight", () => {
  assert.deepEqual([...route([member("a", 0, 3), member("b", 0, 1), member("c", 1)]).shares], [["a", 75], ["b", 25]])
  assert.deepEqual([...route([member("a", 0, 0), member("b", 0, 0)]).shares], [["a", 50], ["b", 50]])
  assert.equal(route([member("a")], new Set(["a"])).priority, null)
})

test("a failing primary cools down and traffic fails over to the standby", () => {
  const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["a"]) })
  assert.deepEqual(states(simulate(sim, "normal")), { a: ["serving", 100, false], b: ["standby", 0, false] })
  assert.deepEqual(states(simulate(sim, "failing")), { a: ["serving", 100, true], b: ["standby", 0, false] })
  const cooled = simulate(sim, "tripped")
  assert.deepEqual(states(cooled), { a: ["tripped", 0, false], b: ["serving", 100, false] })
  assert.equal(cooled.outcome, "ok")
  // Within the request the failed attempt falls through to the next priority, even without retries.
  assert.equal(simulate(sim, "failing").outcome, "ok")
  assert.equal(simulate({ ...sim, retries: 0 }, "failing").outcome, "ok")
  assert.equal(rescue({ ...sim, retries: 0 }, "a"), "fallback")
  assert.deepEqual(keys(narrate(sim, "tripped", String)), ["simTripped", "simFailoverTo", "simClientOk"])
})

test("a balanced pool needs retries to hide a failure", () => {
  const sim = input([member("a"), member("b")], { failing: new Set(["a"]) })
  assert.equal(rescue(sim, "a"), "retry")
  assert.equal(simulate(sim, "failing").outcome, "ok")
  const noRetry = { ...sim, retries: 0 }
  assert.equal(rescue(noRetry, "a"), null)
  assert.equal(simulate(noRetry, "failing").outcome, "degraded")
  assert.ok(keys(narrate(noRetry, "failing", String)).includes("simNoRescue"))
  // Once a cools down, b takes everything.
  assert.deepEqual(states(simulate(sim, "tripped")), { a: ["tripped", 0, false], b: ["serving", 100, false] })
})

test("429s and outages both count toward the cooldown", () => {
  for (const kind of ["throttle", "outage"]) {
    const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["a"]), kind })
    assert.equal(simulate(sim, "tripped").members[0].state, "tripped", kind)
  }
  const lines = narrate(input([member("a", 0), member("b", 1)], {
    failing: new Set(["a"]), breaker: { allowed_fails: 2, cooldown_seconds: 30 },
  }), "failing", String)
  const rule = lines.find((line) => line.key === "simCooldownRule")
  assert.deepEqual(rule.values, { name: "a", count: 3, cooldown: 30 })
})

test("failures cascade through standbys until nothing is left", () => {
  const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["a", "b"]) })
  const cooled = simulate(sim, "tripped")
  assert.deepEqual(states(cooled), { a: ["tripped", 0, false], b: ["tripped", 0, false] })
  assert.equal(cooled.outcome, "down")
  assert.equal(simulate(sim, "failing").outcome, "degraded", "nothing healthy to retry on")
  assert.deepEqual(keys(narrate(sim, "tripped", String)), ["simTripped", "simAllDown"])
})

test("an idle failing standby is not detected and does not cool down", () => {
  const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["b"]) })
  assert.deepEqual(states(simulate(sim, "tripped")), { a: ["serving", 100, false], b: ["standby", 0, true] })
  assert.deepEqual(keys(narrate(sim, "failing", String)), ["simFailing429", "simFailingIdle"])
  assert.deepEqual(keys(narrate(sim, "recovered", String)), ["simNothingTripped"])
})

test("balanced pools shift load inside the group", () => {
  const sim = input([member("a"), member("b"), member("c")], { failing: new Set(["a"]) })
  assert.deepEqual(states(simulate(sim, "tripped")), {
    a: ["tripped", 0, false], b: ["serving", 50, false], c: ["serving", 50, false],
  })
  const lines = narrate(sim, "tripped", String)
  assert.equal(lines[1].key, "simShiftTo")
  assert.deepEqual(lines[1].values.names, ["b 50%", "c 50%"])
})

test("normal narration explains standbys, and recovery the cooldown", () => {
  const sim = input([member("a", 0), member("b", 1)])
  assert.deepEqual(keys(narrate(sim, "normal", String)), ["simServingOne", "simStandby", "simHint"])
  const recovered = narrate({ ...sim, failing: new Set(["a"]) }, "recovered", String)
  assert.deepEqual(keys(recovered), ["simRecovered", "simRetrip"])
  assert.equal(recovered[0].values.cooldown, 60)
})

const pool = (members, extra = {}) => ({ members, title: null, description: null, ...extra })
const pm = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })

test("publish summary lists role, share, membership and settings changes", () => {
  const before = pool([pm("a", 0), pm("b", 1)])
  assert.deepEqual(poolChanges(before, before), [])
  assert.deepEqual(poolChanges(before, pool([pm("a"), pm("b")], { retries: 3 })), [
    { kind: "mode", from: "failover", to: "balanced" },
    { kind: "share", id: "a", from: 100, to: 50 },
    { kind: "role", id: "b", from: "standby", to: "member" },
    { kind: "share", id: "b", from: 0, to: 50 },
    { kind: "settings", count: 2 },
  ])
  assert.deepEqual(poolChanges(before, pool([pm("b", 0), pm("a", 1), pm("c", 1)])), [
    { kind: "role", id: "b", from: "standby", to: "primary" },
    { kind: "share", id: "b", from: 0, to: 100 },
    { kind: "role", id: "a", from: "primary", to: "standby" },
    { kind: "share", id: "a", from: 100, to: 0 },
    { kind: "added", id: "c", role: "standby", share: 0 },
  ])
  assert.deepEqual(poolChanges(pool([pm("a", 0), pm("b", 1), pm("c", 1)]), before), [{ kind: "removed", id: "c" }])
  // The defaults written out are not a change.
  assert.deepEqual(poolChanges(before, pool(before.members, { retries: 1, timeout_seconds: 120 })), [])
  assert.deepEqual(poolChanges(null, before), [
    { kind: "created", mode: "failover", count: 2 },
    { kind: "added", id: "a", role: "primary", share: 100 },
    { kind: "added", id: "b", role: "standby", share: 0 },
  ])
})

test("config diff marks added and removed lines", () => {
  assert.deepEqual(diffLines("a\nb\nc", "a\nx\nc"), [
    { kind: "same", text: "a" },
    { kind: "removed", text: "b" },
    { kind: "added", text: "x" },
    { kind: "same", text: "c" },
  ])
  assert.deepEqual(diffLines("", "a"), [{ kind: "added", text: "a" }])
})
