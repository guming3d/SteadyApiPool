import assert from "node:assert/strict"
import test from "node:test"

import { capacityLimit, clampCapacity, defaultVersion, DEPLOYMENT_NAME_PATTERN, finishedState, modelNames, suggestDeploymentName, versionsOf } from "../src/deploy-model.ts"

const model = (name, version, is_default_version = false) => ({
  name, version, is_default_version, lifecycle_status: null, retires_on: null, capabilities: [], skus: [],
})
const models = [model("gpt-4o", "2024-11-20", true), model("gpt-4o", "2024-08-06"), model("gpt-5", "2026-01-01")]
const sku = { name: "GlobalStandard", default_capacity: 50, min_capacity: 1, max_capacity: 1000, step: 1 }

test("models group their versions and preselect the default", () => {
  assert.deepEqual(modelNames(models), ["gpt-4o", "gpt-5"])
  assert.deepEqual(versionsOf(models, "gpt-4o").map((item) => item.version), ["2024-11-20", "2024-08-06"])
  assert.equal(defaultVersion(models, "gpt-4o").version, "2024-11-20")
  // No version marked default: the first listed.
  assert.equal(defaultVersion(models, "gpt-5").version, "2026-01-01")
  assert.equal(defaultVersion(models, "missing"), undefined)
})

test("the suggested name is free in the account and valid for Azure", () => {
  assert.equal(suggestDeploymentName("gpt-4o", []), "gpt-4o")
  assert.equal(suggestDeploymentName("gpt-4o", ["GPT-4o"]), "gpt-4o-2")
  assert.equal(suggestDeploymentName("gpt-4o", ["gpt-4o", "gpt-4o-2"]), "gpt-4o-3")
  const long = suggestDeploymentName("m".repeat(80), ["m".repeat(64)])
  assert.equal(long.length, 64)
  assert.ok(long.endsWith("-2"))
  for (const name of ["gpt-4o", "gpt-4o-2", long, suggestDeploymentName("o3 mini/preview", [])]) {
    assert.ok(DEPLOYMENT_NAME_PATTERN.test(name), name)
  }
  assert.equal(DEPLOYMENT_NAME_PATTERN.test("-bad"), false)
  assert.equal(DEPLOYMENT_NAME_PATTERN.test("a"), false)
})

test("capacity stays within the type's range and the quota left", () => {
  assert.equal(capacityLimit(sku, undefined), 1000)
  assert.equal(capacityLimit(sku, 450), 450)
  assert.equal(clampCapacity(5000, sku, 450), 450)
  assert.equal(clampCapacity(0, sku), 1)
  assert.equal(clampCapacity(Number.NaN, sku), 50)
  assert.equal(clampCapacity(26, { ...sku, step: 10 }), 30)
  // No quota left: the minimum, which the dialog flags as unavailable.
  assert.equal(clampCapacity(50, sku, 0), 1)
})

test("polling stops at Azure's final states", () => {
  for (const state of ["Succeeded", "Failed", "Canceled"]) assert.equal(finishedState(state), true)
  for (const state of ["Accepted", "Creating"]) assert.equal(finishedState(state), false)
})
