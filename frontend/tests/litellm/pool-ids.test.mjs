import assert from "node:assert/strict"
import test from "node:test"

import { MODEL_NAME_PATTERN, POOL_ID_PATTERN, suggestPoolId } from "../../src/litellm/pool-ids.ts"

test("a new pool's ID comes from its model and is a valid model name", () => {
  assert.equal(suggestPoolId("gpt-4.1"), "gpt-4-1")
  assert.equal(suggestPoolId("  GPT 4o Mini! "), "gpt-4o-mini")
  assert.equal(suggestPoolId(null), "pool")
  assert.equal(suggestPoolId("gpt-4o", ["GPT-4o", "gpt-4o-2"]), "gpt-4o-3")
  for (const id of ["gpt-4-1", "gpt-4o-mini", "pool"]) {
    assert.match(id, POOL_ID_PATTERN)
    assert.match(id, MODEL_NAME_PATTERN)
  }
  assert.match("gpt-4o-2024-11-20", MODEL_NAME_PATTERN)
  assert.match("azure:gpt-4.1", MODEL_NAME_PATTERN)
  assert.doesNotMatch("gpt 4o", MODEL_NAME_PATTERN)
  assert.doesNotMatch("gpt.4", POOL_ID_PATTERN)
})
