import assert from "node:assert/strict"
import test from "node:test"

import { attentionCount, canAdvance, elapsedMinutes, firstStep, reachable, grantable, resourceName, stepChecks, stepState } from "../src/setup-steps.ts"

const check = (id, kind, status, extra = {}) => ({
  id, kind, status, subject: null, missing: [], detail: null, fix: null, request: null, granted_by: null, ...extra,
})

const report = (checks, can_operate = true) => ({ mode: "local", user: null, checks, ready: false, can_operate })

test("checks are grouped into steps", () => {
  const value = report([
    check("sign_in", "sign_in", "ok"),
    check("gateway_access", "gateway_access", "action"),
    check("discovery:sub", "discovery", "ok"),
    check("gateway_identity", "gateway_identity", "ok"),
  ])
  assert.deepEqual(stepChecks(value, "access").map((item) => item.id), ["gateway_access", "discovery:sub"])
  assert.equal(stepState(value, "sign_in"), "ok")
  assert.equal(stepState(value, "access"), "attention")
  assert.equal(stepState(value, "foundry"), "ok")
  assert.equal(attentionCount(value), 1)
  assert.equal(firstStep(value), "access")
})

test("steps after a failed sign-in are waiting", () => {
  const value = report([check("sign_in", "sign_in", "action", { fix: "sign_in" })], false)
  assert.equal(stepState(value, "access"), "waiting")
  assert.equal(stepState(value, "foundry"), "waiting")
  assert.equal(firstStep(value), "sign_in")
})

test("pending is not attention but keeps the step open", () => {
  const value = report([
    check("sign_in", "sign_in", "ok"),
    check("gateway_access", "gateway_access", "ok"),
    check("gateway_identity", "gateway_identity", "pending"),
  ])
  assert.equal(stepState(value, "foundry"), "pending")
  assert.equal(attentionCount(value), 0)
  assert.equal(firstStep(value), "foundry")
})

test("without a gateway the wizard opens on choosing one", () => {
  const value = report([
    check("sign_in", "sign_in", "ok"),
    check("gateway_target", "gateway_target", "action", { fix: "select_gateway" }),
  ], false)
  assert.equal(stepState(value, "gateway"), "attention")
  assert.equal(stepState(value, "access"), "waiting")
  assert.equal(stepState(value, "foundry"), "waiting")
  assert.equal(firstStep(value), "gateway")
})

test("a gateway being deployed keeps the gateway step pending", () => {
  const value = report([
    check("sign_in", "sign_in", "ok"),
    check("gateway_target", "gateway_target", "pending", { subject: "gw-new", detail: "Deploying" }),
  ], false)
  assert.equal(stepState(value, "gateway"), "pending")
  assert.equal(attentionCount(value), 0)
  assert.equal(firstStep(value), "gateway")
})

test("a ready report opens on the last step", () => {
  const value = report([check("sign_in", "sign_in", "ok"), check("gateway_access", "gateway_access", "ok")])
  assert.equal(firstStep(value), "foundry")
})

test("grantable lists accounts the user can fix", () => {
  const account = "/subscriptions/s/resourceGroups/r/providers/Microsoft.CognitiveServices/accounts/east"
  const checks = [
    check("a", "foundry_access", "action", { fix: "grant_foundry_access", subject: account }),
    check("b", "foundry_access", "action", { subject: "other" }),
  ]
  assert.deepEqual(grantable(checks), [account])
  assert.equal(resourceName(account), "east")
  assert.equal(resourceName(null), "")
})

test("provisioning time is read from ARM's timestamp", () => {
  const now = Date.parse("2026-10-25T10:12:30Z")
  assert.equal(elapsedMinutes("2026-10-25T10:00:00.1234567Z", now), 12)
  assert.equal(elapsedMinutes("2026-10-25T10:00:00", now), 12, "no zone means UTC")
  assert.equal(elapsedMinutes("2026-10-25T18:00:00+08:00", now), 12)
  assert.equal(elapsedMinutes("2026-10-25T10:12:50Z", now), 0, "a clock behind Azure's never goes negative")
  assert.equal(elapsedMinutes(null, now), null)
  assert.equal(elapsedMinutes("soon", now), null)
  assert.equal(elapsedMinutes("0001-01-01T00:00:00", now), null, "Azure's placeholder date")
})

test("Next waits while the gateway is deploying or not chosen, and before sign-in", () => {
  const signedIn = check("sign_in", "sign_in", "ok")
  const provisioning = { mode: "local", checks: [signedIn, check("gateway_target", "gateway_target", "pending")], ready: false, can_operate: false }
  assert.equal(canAdvance(provisioning, "sign_in"), true)
  assert.equal(canAdvance(provisioning, "gateway"), false)
  assert.equal(reachable(provisioning, "access", "gateway"), false)
  assert.equal(reachable(provisioning, "foundry", "gateway"), false)
  // Wherever it opened, the current step stays open.
  assert.equal(reachable(provisioning, "access", "access"), true)

  const unchosen = { ...provisioning, checks: [signedIn, check("gateway_target", "gateway_target", "action")] }
  assert.equal(canAdvance(unchosen, "gateway"), false)

  const signedOut = { mode: "local", checks: [check("sign_in", "sign_in", "action")], ready: false, can_operate: false }
  assert.equal(canAdvance(signedOut, "sign_in"), false)
})

test("Next moves on once the gateway is ready, even past advisory items", () => {
  const report = { mode: "local", ready: false, can_operate: true, checks: [
    check("sign_in", "sign_in", "ok"),
    check("gateway_target", "gateway_target", "ok"),
    check("gateway_access", "gateway_access", "action"),
    check("gateway_identity", "gateway_identity", "ok"),
  ] }
  assert.equal(canAdvance(report, "gateway"), true)
  assert.equal(canAdvance(report, "access"), true)
  assert.equal(canAdvance(report, "foundry"), false, "no step after the last one")
})

test("an unreachable gateway needs attention in the gateway step", () => {
  const value = report([
    check("sign_in", "sign_in", "ok"),
    check("gateway_target", "gateway_target", "ok"),
    check("gateway_access", "gateway_access", "ok"),
    check("gateway_health", "gateway_health", "blocked", { detail: "master_key_refused" }),
  ], false)
  assert.equal(stepState(value, "gateway"), "attention")
  assert.equal(firstStep(value), "gateway")
})
