import assert from "node:assert/strict"
import test from "node:test"

import { API_PATH_PATTERN, poolPayload, suggestApiPath } from "../../src/apim/api-paths.ts"

const member = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })

test("unsupported affinity cannot silently downgrade to ordinary balancing", () => {
  const draft = { members: [member("a"), member("b")], session_affinity: true }
  assert.throws(() => poolPayload(draft, false))
  assert.equal(poolPayload({ ...draft, session_affinity: false }, false).session_affinity, false)
  assert.equal(poolPayload(draft, true).session_affinity, true)
})

test("new pool APIs get a readable path that passes APIM's path rule", () => {
  assert.equal(suggestApiPath("gpt-4.1", "pool-gpt-4-1", "pool-"), "gpt-4.1")
  assert.equal(suggestApiPath("  GPT 4o Mini! ", "x", "pool-"), "gpt-4o-mini")
  assert.equal(suggestApiPath(null, "pool-gpt-4o", "pool-"), "gpt-4o")
  assert.equal(suggestApiPath("", "gpt-4o", "pool-"), "gpt-4o")
  assert.equal(suggestApiPath("-._", "pool-", "pool-"), "pool")
  for (const path of ["gpt-4.1", "gpt-4o-mini", "gpt-4o", "pool"]) assert.match(path, API_PATH_PATTERN)
  assert.doesNotMatch("/gpt", API_PATH_PATTERN)
  assert.doesNotMatch("a b", API_PATH_PATTERN)
})
