import assert from "node:assert/strict"
import test from "node:test"

import { isGatewayName, suggestGatewayName } from "../../src/litellm/gateway-choice.ts"

test("gateway names follow the backend's rule", () => {
  for (const name of ["ai-gateway", "gw2", suggestGatewayName(() => 0.123456)]) assert.equal(isGatewayName(name), true, name)
  for (const name of ["Gateway", "g", "gw-", "gw--x", "1gw", "a".repeat(33)]) assert.equal(isGatewayName(name), false, name)
})
