import assert from "node:assert/strict"
import test from "node:test"

import { capacityWeights, formatRate, quotaSummary, sizedFor, splitLimit } from "../src/pool-quota.ts"

const member = (overrides) => ({
  backend_id: "m",
  deployment_id: "/d",
  model_name: "gpt-6",
  model_version: null,
  problem: null,
  deployment_type: "GlobalStandard",
  capacity: 100,
  tpm: 100_000,
  rpm: 600,
  quota_source: "rate_limits",
  ...overrides,
})
const check = (members, total_tpm, total_rpm) => ({ members, model_names: ["gpt-6"], ok: true, message: null, total_tpm, total_rpm })

test("a scenario becomes tokens and requests a minute at its peak", () => {
  assert.deepEqual(sizedFor({ peak_rpm: 6000, input_tokens: 2000, output_tokens: 500 }), { tpm: 15_000_000, rpm: 6000 })
  assert.equal(sizedFor(null), null)
})

test("the pool's totals, and whether they cover the sized peak", () => {
  const pool = check([member(), member({ backend_id: "n" })], 200_000, 1200)

  assert.deepEqual(quotaSummary(pool, null), { tpm: 200_000, rpm: 1200, estimated: false, unknown: 0, short: false })
  assert.equal(quotaSummary(pool, { tpm: 15_000_000, rpm: 6000 }).short, true)
  assert.equal(quotaSummary(pool, { tpm: 150_000, rpm: 1000 }).short, false)
  // Enough tokens but not enough requests is short too.
  assert.equal(quotaSummary(pool, { tpm: 100_000, rpm: 2000 }).short, true)
})

test("estimates and unknown members are said", () => {
  const pool = check([member({ quota_source: "capacity" }), member({ tpm: null, rpm: null, quota_source: null })], 100_000, 600)
  const summary = quotaSummary(pool, null)

  assert.equal(summary.estimated, true)
  assert.equal(summary.unknown, 1)
})

test("nothing to say without any limits", () => {
  assert.equal(quotaSummary(undefined, null), null)
  assert.equal(quotaSummary(check([member({ tpm: null, rpm: null })], null, null), null), null)
})

test("rates read as quota is quoted", () => {
  assert.equal(formatRate(15_000_000, "en"), "15M")
  assert.equal(formatRate(200_000, "en"), "200K")
  assert.equal(formatRate(1200, "en"), "1,200")
})

test("how the members are set decides what the pool takes before its first 429", () => {
  // 2.5M, 5M and 10K tokens a minute (rpm alike), as in a real gpt-4.1-mini pool.
  const pool = check([
    member({ backend_id: "big", tpm: 5_000_000, rpm: 5000 }),
    member({ backend_id: "mid", tpm: 2_500_000, rpm: 2500 }),
    member({ backend_id: "tiny", tpm: 10_000, rpm: 10 }),
  ], 7_510_000, 7510)
  const failover = [
    { backend_id: "mid", priority: 0, weight: 1 },
    { backend_id: "big", priority: 1, weight: 1 },
    { backend_id: "tiny", priority: 2, weight: 1 },
  ]
  const balanced = failover.map((item) => ({ ...item, priority: 0 }))
  const weighted = [
    { backend_id: "big", priority: 0, weight: 2 },
    { backend_id: "mid", priority: 0, weight: 1 },
    { backend_id: "tiny", priority: 0, weight: 0 },
  ]

  // Failover: the primary's own limit.
  assert.deepEqual(splitLimit(pool, failover), { tpm: 2_500_000, rpm: 2500, active: 1 })
  // Balanced: a third each, so the smallest deployment is rate-limited first.
  assert.deepEqual(splitLimit(pool, balanced), { tpm: 30_000, rpm: 30, active: 3 })
  // Weighted 2:1 (and 0, which takes nothing): both reach their limits together.
  assert.deepEqual(splitLimit(pool, weighted), { tpm: 7_500_000, rpm: 7500, active: 3 })
  // Members are matched whatever the case of their IDs.
  assert.equal(splitLimit(pool, [{ backend_id: "BIG", priority: 0, weight: 1 }]).tpm, 5_000_000)
})

test("an active member without limits leaves the split unknown, a standby one doesn't", () => {
  const pool = check([member({ backend_id: "a" }), member({ backend_id: "custom", tpm: null, rpm: null, quota_source: null })], 100_000, 600)

  assert.deepEqual(splitLimit(pool, [{ backend_id: "a", priority: 0, weight: 1 }, { backend_id: "custom", priority: 0, weight: 1 }]), { tpm: null, rpm: null, active: 2 })
  assert.deepEqual(splitLimit(pool, [{ backend_id: "a", priority: 0, weight: 1 }, { backend_id: "custom", priority: 1, weight: 1 }]), { tpm: 100_000, rpm: 600, active: 1 })
  assert.equal(splitLimit(undefined, []), null)
})

test("weights by capacity follow each deployment's tokens a minute", () => {
  const pool = check([
    member({ backend_id: "big", tpm: 5_000_000, rpm: 5000 }),
    member({ backend_id: "mid", tpm: 2_500_000, rpm: 2500 }),
    member({ backend_id: "tiny", tpm: 10_000, rpm: 10 }),
  ], 7_510_000, 7510)
  const members = ["big", "mid", "tiny"].map((backend_id) => ({ backend_id, priority: 0, weight: 1 }))

  // The largest gets 100; none goes below 1, the least a weight takes.
  assert.deepEqual(capacityWeights(pool, members).map((item) => item.weight), [100, 50, 1])
  // Equal deployments would be balanced, not weighted.
  const even = check([member({ backend_id: "a" }), member({ backend_id: "b" })], 200_000, 1200)
  assert.equal(capacityWeights(even, [{ backend_id: "a", priority: 0, weight: 3 }, { backend_id: "b", priority: 0, weight: 1 }]), null)
  // A member without limits leaves it to the user.
  const custom = check([member({ backend_id: "a" }), member({ backend_id: "c", tpm: null, rpm: null })], 100_000, 600)
  assert.equal(capacityWeights(custom, [{ backend_id: "a", priority: 0, weight: 1 }, { backend_id: "c", priority: 0, weight: 1 }]), null)
})
