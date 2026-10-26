import assert from "node:assert/strict"
import test from "node:test"

import { parseStoredTarget, targetFromServiceId, targetFromView, targetHeader, targetStorageKey } from "../src/public-target.ts"

const target = { subscription_id: "11111111-1111-1111-1111-111111111111", resource_group: "rg-ai", gateway_name: "ai-gateway", tenant_id: "t1" }

test("the header carries the three parts, URL-encoded", () => {
  assert.equal(targetHeader(target), "11111111-1111-1111-1111-111111111111/rg-ai/ai-gateway")
  assert.equal(targetHeader({ ...target, resource_group: "résumé(1)" }), "11111111-1111-1111-1111-111111111111/r%C3%A9sum%C3%A9(1)/ai-gateway")
})

test("a chosen gateway comes from the target view or its Container App ID", () => {
  assert.deepEqual(targetFromView({ ...target, gateway_kind: "aca", gateway_url: null, source: "browser", can_change: true }), target)
  assert.equal(targetFromView({
    subscription_id: null, resource_group: null, gateway_name: null, gateway_kind: null, gateway_url: null,
    tenant_id: null, source: "none", can_change: true,
  }), null)
  const id = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-ai/providers/Microsoft.App/containerApps/ai-gateway"
  assert.deepEqual(targetFromServiceId(id, "t1"), target)
  assert.equal(targetFromServiceId("/subscriptions/x/resourceGroups/y", null), null)
})

test("stored targets are checked before use", () => {
  assert.deepEqual(parseStoredTarget(JSON.stringify(target)), target)
  assert.deepEqual(parseStoredTarget(JSON.stringify({ ...target, tenant_id: undefined })), { ...target, tenant_id: null })
  for (const raw of [null, "", "not json", "[]", JSON.stringify({ ...target, gateway_name: "" })]) {
    assert.equal(parseStoredTarget(raw), null)
  }
  assert.equal(targetStorageKey("oid.tid"), "ai-gateway-pool:target:oid.tid")
})
