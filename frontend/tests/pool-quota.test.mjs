import assert from "node:assert/strict"
import test from "node:test"

import { formatRate, quotaSummary, sizedFor } from "../src/pool-quota.ts"

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
