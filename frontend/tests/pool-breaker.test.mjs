import assert from "node:assert/strict"
import test from "node:test"

import { DEFAULT_BREAKER, followsPool, membersToUpdate, poolBreaker, readOnlyMembers, sameBreaker, withPoolBreaker } from "../src/pool-breaker.ts"

const tight = { ...DEFAULT_BREAKER, failure_count: 5, trip_duration_seconds: 120 }
const backend = (id, circuit_breaker, extra = {}) => ({
  id, title: null, description: null, type: "Single", url: `https://${id}.example.com`, auth_type: "managed_identity",
  header_name: null, named_value: null, circuit_breaker, has_circuit_breaker: circuit_breaker !== null,
  managed: true, protected: false, etag: null, ...extra,
})
const byId = (...backends) => new Map(backends.map((item) => [item.id.toLowerCase(), item]))

test("only members this app created follow the pool's rule", () => {
  assert.equal(followsPool(backend("a", tight)), true)
  assert.equal(followsPool(backend("b", tight, { managed: false })), false)
  assert.equal(followsPool(backend("other-x", tight, { protected: true })), false)
  assert.equal(followsPool(undefined), false)
})

test("rules compare field by field, in order", () => {
  assert.equal(sameBreaker(DEFAULT_BREAKER, { ...DEFAULT_BREAKER }), true)
  assert.equal(sameBreaker(DEFAULT_BREAKER, tight), false)
  assert.equal(sameBreaker(DEFAULT_BREAKER, { ...DEFAULT_BREAKER, error_reasons: ["Timeout"] }), false)
  assert.equal(sameBreaker(null, null), true)
  assert.equal(sameBreaker(null, DEFAULT_BREAKER), false)
})

test("a pool starts from the rule most of its managed members share", () => {
  const backends = byId(backend("a", tight), backend("b", tight), backend("c", DEFAULT_BREAKER), backend("x", DEFAULT_BREAKER, { managed: false }))
  assert.deepEqual(poolBreaker(["a", "b", "c", "x"], backends), { rule: tight, uniform: false })
  // External members don't vote, and a tie goes to the primary.
  assert.deepEqual(poolBreaker(["c", "a", "x"], backends), { rule: DEFAULT_BREAKER, uniform: false })
  assert.deepEqual(poolBreaker(["a", "b"], backends), { rule: tight, uniform: true })
  // No readable rule among managed members: the default, and publishing will set it.
  assert.deepEqual(poolBreaker(["n"], byId(backend("n", null))), { rule: DEFAULT_BREAKER, uniform: false })
  assert.deepEqual(poolBreaker([], new Map()), { rule: DEFAULT_BREAKER, uniform: true })
})

test("publishing rewrites only managed members whose rule differs", () => {
  const backends = byId(backend("a", tight), backend("b", DEFAULT_BREAKER), backend("n", null), backend("x", null, { managed: false }))
  assert.deepEqual(membersToUpdate(["a", "b", "n", "x", "gone"], backends, tight), ["b", "n"])
  assert.deepEqual(readOnlyMembers(["a", "x", "gone"], backends), ["x"])
})

test("the preview sees the rule members will have after publishing", () => {
  const backends = byId(backend("b", DEFAULT_BREAKER), backend("x", null, { managed: false }))
  const next = withPoolBreaker(backends, ["B", "x"], tight)
  assert.deepEqual(next.get("b").circuit_breaker, tight)
  assert.equal(next.get("x").has_circuit_breaker, false)
  // The input map is left alone.
  assert.deepEqual(backends.get("b").circuit_breaker, DEFAULT_BREAKER)
})
