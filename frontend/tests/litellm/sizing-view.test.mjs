import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import test from "node:test"

import { DEFAULT_SIZING, LIMITS, formatTpm, invalidFields, noteKey, noteValues, parseCount, planSteps, planSummary, resizeChangesSomething, resizeImpacts } from "../../src/litellm/sizing-view.ts"

// The backend's plan for 6,000 RPM, 2,000 tokens in, 500 out, streamed (test_sizing.py's worked example).
const PLAN = {
  platform: "aca",
  profile: "production",
  rps: 100,
  tpm: 15_000_000,
  call_seconds: 9.53,
  in_flight: 954,
  in_flight_slow: 12_000,
  cpu_ms: 24.25,
  replica_rps: 28.9,
  replica_in_flight: 1000,
  bound_by: "cpu",
  peak_replicas: 4,
  min_replicas: 2,
  max_replicas: 6,
  scale_concurrency: 25,
  scale_cpu_percent: 27,
  replica_cpu: 2,
  replica_memory_gib: 4,
  bandwidth_mbps: 16,
  postgres_sku: "Standard_D2ds_v5",
  postgres_tier: "GeneralPurpose",
  postgres_connections: 120,
  postgres_limit: 844,
  db_pool: 10,
  redis_sku: "Balanced_B0",
  redis_buffer: false,
  aks: null,
  request_timeout: 120,
  route_timeout: 600,
  reasons: [{ code: "platform_simple", values: {} }],
  warnings: [{ code: "foundry_quota", values: { tpm: 15_000_000, rpm: 6000 } }],
  problems: [],
}
const SIZING = { ...DEFAULT_SIZING, peak_rpm: 6000 }

test("the defaults are a valid scenario", () => {
  assert.deepEqual(invalidFields(DEFAULT_SIZING), [])
})

test("fields outside the backend's bounds are named", () => {
  assert.deepEqual(invalidFields({ ...DEFAULT_SIZING, peak_rpm: 0, timeout_seconds: 5 }), ["peak_rpm", "timeout_seconds"])
  assert.deepEqual(invalidFields({ ...DEFAULT_SIZING, input_tokens: Number.NaN }), ["input_tokens"])
  assert.deepEqual(invalidFields({ ...DEFAULT_SIZING, output_tokens: 2.5 }), ["output_tokens"])
  assert.deepEqual(invalidFields({ ...DEFAULT_SIZING, response_seconds: 0 }), ["response_seconds"])
  assert.deepEqual(invalidFields({ ...DEFAULT_SIZING, response_seconds: 12.5 }), [])
})

test("the bounds mirror GatewaySizing in the backend", () => {
  const models = readFileSync(new URL("../../../backend/pool_manager/litellm/models.py", import.meta.url), "utf8")
  const sizing = models.slice(models.indexOf("class GatewaySizing"), models.indexOf("\nclass ", models.indexOf("class GatewaySizing")))
  for (const [field, { min, max }] of Object.entries(LIMITS)) {
    const line = sizing.split("\n").find((text) => text.trimStart().startsWith(`${field}: int`))
    assert.ok(line, field)
    const bound = (name) => Number(line.match(new RegExp(`${name}=([\\d_]+)`))?.[1].replaceAll("_", ""))
    assert.deepEqual([bound("ge"), bound("le")], [min, max], field)
  }
})

test("typed counts accept separators, and empty is not a number", () => {
  assert.equal(parseCount("6,000"), 6000)
  assert.equal(parseCount("1 000 000"), 1_000_000)
  assert.ok(Number.isNaN(parseCount("lots")))
})

test("tokens a minute read as Foundry quota does", () => {
  assert.equal(formatTpm(15_000_000, "en"), "15M")
  assert.equal(formatTpm(1_250_000, "en"), "1.3M")
  assert.equal(formatTpm(78_000, "en"), "78K")
})

test("the steps carry the plan's numbers in order", () => {
  const steps = planSteps(PLAN, SIZING, "en")

  assert.deepEqual(steps.map((step) => step.key), [
    "planStepRps", "planStepTpm", "planStepCall", "planStepInFlight", "planStepCpu", "planStepHolds",
    "planStepReplicas", "planStepScaleAca", "planStepDatabase", "planStepRedis", "planStepBandwidth", "planStepTimeoutAca",
  ])
  assert.deepEqual(steps[0].values, { rps: "100", rpm: "6,000" })
  assert.equal(steps[1].values.tpm, "15M")
  assert.deepEqual(steps[6].values, { peak: "4", range: "2–6", headroom: "25" })
  assert.equal(steps[8].values.sku, "D2ds_v5")
})

test("an AKS plan has nodes and outbound ports instead of the replica trigger", () => {
  const aks = {
    ...PLAN,
    platform: "aks",
    aks: { node_size: "Standard_D4ds_v5", node_vcpus: 4, pods_per_node: 2, node_min: 3, node_max: 3, tier: "Standard", outbound_ips: 1, outbound_ports: 16_000, ports_needed: 4000 },
  }
  const keys = planSteps(aks, { ...SIZING, zone_redundant: false }, "en").map((step) => step.key)

  assert.ok(keys.includes("planStepNodes") && keys.includes("planStepOutbound") && keys.includes("planStepScaleAks"))
  assert.ok(!keys.includes("planStepScaleAca"))
  assert.ok(keys.includes("planStepReplicasSingle"))
  assert.equal(planSummary(aks, "en").nodes, "3 × D4ds_v5")
})

test("a plan sums up in one line", () => {
  assert.deepEqual(planSummary(PLAN, "en"), { replicas: "2–6", nodes: "", database: "D2ds_v5", redis: "Balanced B0" })
  assert.equal(planSummary({ ...PLAN, min_replicas: 1, max_replicas: 1, redis_sku: null }, "en").replicas, "1")
})

test("notes read by their code, numbers formatted", () => {
  const [quota] = PLAN.warnings
  assert.equal(noteKey(quota), "planNote_foundry_quota")
  assert.deepEqual(noteValues(quota, "en"), { tpm: "15M", rpm: "6,000" })
})

test("a resize warns about what it does to running traffic", () => {
  const change = (item, action) => ({ item, current: "1", proposed: "2", action, command: null })
  const aca = [change("replicas", "apply"), change("database", "optional"), change("redis", "manual")]
  const aks = [change("nodes", "apply"), change("pods", "apply")]

  assert.deepEqual(resizeImpacts(aca, false), ["resizeImpact_revision"])
  assert.deepEqual(resizeImpacts(aca, true), ["resizeImpact_revision", "resizeImpact_database"])
  assert.deepEqual(resizeImpacts(aks, false), ["resizeImpact_nodes", "resizeImpact_pods"])
  // Only what the app would change counts; a manual item changes nothing.
  assert.equal(resizeChangesSomething([change("redis", "manual")], false), false)
  assert.equal(resizeChangesSomething([change("database", "optional")], true), true)
})
