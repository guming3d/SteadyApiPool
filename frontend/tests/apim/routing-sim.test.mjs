import assert from "node:assert/strict"
import test from "node:test"

import { diffLines, managedLineFlags } from "../../src/apim/policy-diff.ts"
import { poolChanges } from "../../src/apim/pool-changes.ts"
import { breakerCatches, narrate, route, simulate } from "../../src/apim/routing-sim.ts"

const breaker = (overrides = {}) => ({
  failure_count: 1,
  interval_seconds: 60,
  trip_duration_seconds: 60,
  accept_retry_after: true,
  status_code_ranges: [{ minimum: 429, maximum: 429 }, { minimum: 408, maximum: 408 }, { minimum: 500, maximum: 599 }],
  error_reasons: ["BackendConnectionFailure", "Timeout"],
  ...overrides,
})
const member = (backend_id, priority = 0, weight = 1, cb = breaker()) => ({ backend_id, priority, weight, breaker: cb })
const input = (members, overrides = {}) => ({
  members,
  retry: { attempts: 2, interval: 1, firstFast: true },
  bound: true,
  sessionAffinity: false,
  failing: new Set(),
  kind: "throttle",
  ...overrides,
})
const states = (result) => Object.fromEntries(result.members.map((m) => [m.backend_id, [m.state, Math.round(m.share), m.failing]]))
const keys = (lines) => lines.map((line) => line.key)

test("traffic goes to the highest priority group and splits by weight", () => {
  assert.deepEqual([...route([member("a", 0, 3), member("b", 0, 1), member("c", 1)]).shares], [["a", 75], ["b", 25]])
  assert.deepEqual([...route([member("a", 0, 0), member("b", 0, 0)]).shares], [["a", 50], ["b", 50]])
  assert.equal(route([member("a")], new Set(["a"])).priority, null)
})

test("a failing primary trips and traffic fails over to the standby", () => {
  const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["a"]) })
  assert.deepEqual(states(simulate(sim, "normal")), { a: ["serving", 100, false], b: ["standby", 0, false] })
  assert.deepEqual(states(simulate(sim, "failing")), { a: ["serving", 100, true], b: ["standby", 0, false] })
  const tripped = simulate(sim, "tripped")
  assert.deepEqual(states(tripped), { a: ["tripped", 0, false], b: ["serving", 100, false] })
  assert.equal(tripped.outcome, "ok")
  // A count-1 breaker plus the 429 retry hides the failure from the client.
  assert.equal(simulate(sim, "failing").outcome, "ok")
  assert.equal(simulate({ ...sim, retry: null }, "failing").outcome, "degraded")
  assert.equal(simulate({ ...sim, kind: "outage" }, "failing").outcome, "degraded")
  assert.deepEqual(keys(narrate(sim, "tripped", String)), ["simTripped", "simFailoverTo", "simClientOk"])
})

test("a member without a breaker keeps receiving traffic and blocks failover", () => {
  const sim = input([member("a", 0, 1, null), member("b", 1)], { failing: new Set(["a"]) })
  const tripped = simulate(sim, "tripped")
  assert.deepEqual(states(tripped), { a: ["serving", 100, true], b: ["standby", 0, false] })
  assert.equal(tripped.outcome, "degraded")
  assert.ok(keys(narrate(sim, "normal", String)).includes("simNoBreaker"))
  assert.ok(keys(narrate(sim, "tripped", String)).includes("simStillFailing"))
})

test("breakers only count the failures their rule lists", () => {
  const only5xx = breaker({ status_code_ranges: [{ minimum: 500, maximum: 599 }], error_reasons: [] })
  assert.equal(breakerCatches(only5xx, "throttle"), false)
  assert.equal(breakerCatches(only5xx, "outage"), true)
  assert.equal(breakerCatches(breaker({ status_code_ranges: [], error_reasons: ["ClientConnectionFailure"] }), "outage"), false)
  const sim = input([member("a", 0, 1, only5xx), member("b", 1)], { failing: new Set(["a"]) })
  assert.equal(simulate(sim, "tripped").members[0].state, "serving")
  assert.ok(keys(narrate(sim, "failing", String)).includes("simBreakerMisses"))
})

test("failures cascade through standbys until nothing is left", () => {
  const sim = input([member("a", 0), member("b", 1)], { failing: new Set(["a", "b"]) })
  const tripped = simulate(sim, "tripped")
  assert.deepEqual(states(tripped), { a: ["tripped", 0, false], b: ["tripped", 0, false] })
  assert.equal(tripped.outcome, "down")
  assert.deepEqual(keys(narrate(sim, "tripped", String)), ["simTripped", "simAllDown"])
})

test("an idle failing standby is not detected and does not trip", () => {
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

test("normal narration explains binding, standbys and affinity", () => {
  const sim = input([member("a", 0), member("b", 1)], { bound: false, retry: null, sessionAffinity: true })
  assert.deepEqual(keys(narrate(sim, "normal", String)), ["simNotBound", "simServingOne", "simStandby", "simAffinity", "simHint"])
})

const pool = (members, extra = {}) => ({ members, session_affinity: false, title: null, description: null, ...extra })
const pm = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })

test("publish summary lists role, share, membership and affinity changes", () => {
  const before = pool([pm("a", 0), pm("b", 1)])
  assert.deepEqual(poolChanges(before, before), [])
  assert.deepEqual(poolChanges(before, pool([pm("a"), pm("b")], { session_affinity: true })), [
    { kind: "mode", from: "failover", to: "balanced" },
    { kind: "share", id: "a", from: 100, to: 50 },
    { kind: "role", id: "b", from: "standby", to: "member" },
    { kind: "share", id: "b", from: 0, to: 50 },
    { kind: "affinity", to: true },
  ])
  assert.deepEqual(poolChanges(before, pool([pm("b", 0), pm("a", 1), pm("c", 1)])), [
    { kind: "role", id: "b", from: "standby", to: "primary" },
    { kind: "share", id: "b", from: 0, to: 100 },
    { kind: "role", id: "a", from: "primary", to: "standby" },
    { kind: "share", id: "a", from: 100, to: 0 },
    { kind: "added", id: "c", role: "standby", share: 0 },
  ])
  assert.deepEqual(poolChanges(pool([pm("a", 0), pm("b", 1), pm("c", 1)]), before), [{ kind: "removed", id: "c" }])
  assert.deepEqual(poolChanges(null, before), [
    { kind: "created", mode: "failover", count: 2 },
    { kind: "added", id: "a", role: "primary", share: 100 },
    { kind: "added", id: "b", role: "standby", share: 0 },
  ])
})

test("policy diff marks added and removed lines", () => {
  assert.deepEqual(diffLines("a\nb\nc", "a\nx\nc"), [
    { kind: "same", text: "a" },
    { kind: "removed", text: "b" },
    { kind: "added", text: "x" },
    { kind: "same", text: "c" },
  ])
  assert.deepEqual(diffLines("", "a"), [{ kind: "added", text: "a" }])
  assert.deepEqual(
    managedLineFlags("<inbound>\n<!-- apim-backend-pool:inbound:begin -->\n<x />\n<!-- apim-backend-pool:inbound:end -->\n</inbound>"),
    [false, true, true, true, false],
  )
})
