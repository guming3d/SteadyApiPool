import assert from "node:assert/strict"
import test from "node:test"

import { ACA_MAX_REPLICAS, DEFAULT_ANSWERS, isGatewayName, peakRps, recommend, suggestGatewayName } from "../src/gateway-choice.ts"

test("a trial without special needs gets a dev gateway on Container Apps", () => {
  const result = recommend(DEFAULT_ANSWERS)
  assert.equal(result.platform, "aca")
  assert.equal(result.profile, "dev")
  assert.equal(result.replicas, 1)
  assert.deepEqual(result.reasons, ["choiceReasonSimple", "choiceReasonTrial"])
})

test("production traffic plans replicas from the peak", () => {
  // 50M tokens a minute at 50k tokens a request: about 17 requests a second.
  const result = recommend({ ...DEFAULT_ANSWERS, stage: "production", peakTpmMillions: 50, tokensPerRequest: 50_000 })
  assert.equal(result.platform, "aca")
  assert.equal(result.profile, "production")
  assert.ok(Math.abs(result.rps - 16.67) < 0.01)
  assert.equal(result.replicas, 2, "at least two, for zone failures")
  assert.ok(result.reasons.includes("choiceReasonProduction"))
})

test("a Kubernetes team, GPU models or very high traffic point to AKS", () => {
  assert.equal(recommend({ ...DEFAULT_ANSWERS, kubernetesTeam: true }).platform, "aks")
  assert.equal(recommend({ ...DEFAULT_ANSWERS, selfHostedModels: true }).reasons[0], "choiceReasonGpu")
  const busy = recommend({ ...DEFAULT_ANSWERS, stage: "production", peakTpmMillions: 1000, tokensPerRequest: 2000 })
  assert.equal(busy.platform, "aks")
  assert.ok(busy.reasons.includes("choiceReasonScale"))
  assert.equal(busy.replicas, 30, "capped")
})

test("Container Apps is only recommended for what its production profile can run", () => {
  for (const tokensPerRequest of [10_000, 5_000, 3_400, 2_000, 1_000]) {
    const result = recommend({ ...DEFAULT_ANSWERS, stage: "production", peakTpmMillions: 15, tokensPerRequest })
    if (result.platform === "aca") assert.ok(result.replicas <= ACA_MAX_REPLICAS, `${tokensPerRequest}: ${result.replicas}`)
  }
  // 15M tokens a minute at 1,000 tokens a request: 250 requests a second, five replicas.
  assert.equal(recommend({ ...DEFAULT_ANSWERS, stage: "production", peakTpmMillions: 15, tokensPerRequest: 1_000 }).platform, "aca")
  assert.equal(recommend({ ...DEFAULT_ANSWERS, stage: "production", peakTpmMillions: 16, tokensPerRequest: 1_000 }).platform, "aks")
})

test("private networking is noted for either platform", () => {
  assert.ok(recommend({ ...DEFAULT_ANSWERS, privateNetwork: true }).reasons.includes("choiceReasonPrivateManual"))
  assert.ok(recommend({ ...DEFAULT_ANSWERS, privateNetwork: true, kubernetesTeam: true }).reasons.includes("choiceReasonPrivateAks"))
})

test("odd inputs don't break the estimate", () => {
  assert.equal(peakRps({ peakTpmMillions: -1, tokensPerRequest: 0 }), 0)
  assert.ok(Number.isFinite(peakRps({ peakTpmMillions: 1, tokensPerRequest: 0 })))
})

test("gateway names follow the backend's rule", () => {
  for (const name of ["ai-gateway", "gw2", suggestGatewayName(() => 0.123456)]) assert.equal(isGatewayName(name), true, name)
  for (const name of ["Gateway", "g", "gw-", "gw--x", "1gw", "a".repeat(33)]) assert.equal(isGatewayName(name), false, name)
})
