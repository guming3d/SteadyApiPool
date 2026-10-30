import assert from "node:assert/strict"
import test from "node:test"

import { keptAcrossSwitch } from "../src/gateway-switch.ts"

test("a switch keeps who is signed in, the tenant lists, the target and the setup report", () => {
  for (const key of [["me"], ["setup"], ["setup-progress"], ["target"], ["target", "tenants"], ["target", "subscriptions", "t1"]]) {
    assert.equal(keptAcrossSwitch(key), true, JSON.stringify(key))
  }
})

test("a switch drops everything read from or for the old gateway", () => {
  for (const key of [
    ["litellm", "pools"], ["apim", "backends"], ["litellm", "metrics", "1h"], ["apim", "target", "services", "t1", "s1"],
    ["deployments"], ["foundry", "accounts"], ["setup", "foundry", "acc"], ["delete-check", "id"],
  ]) {
    assert.equal(keptAcrossSwitch(key), false, JSON.stringify(key))
  }
})
