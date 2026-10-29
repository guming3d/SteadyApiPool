import assert from "node:assert/strict"
import test from "node:test"

import { hasDrafts, keepDraft, takeDraft } from "../src/pool-drafts.ts"

test("a draft comes back once, on the pool version it was made on", () => {
  keepDraft("apim", "Pool-A", '"v1"', { members: ["a", "b", "c"] })
  assert.ok(hasDrafts())
  assert.deepEqual(takeDraft("apim", "pool-a", '"v1"'), { value: { members: ["a", "b", "c"] } })
  assert.equal(takeDraft("apim", "pool-a", '"v1"'), null)
  assert.ok(!hasDrafts())
})

test("a draft on a pool that changed since is dropped as stale", () => {
  keepDraft("litellm", "pool-b", "hash-1", { members: ["a"] })
  assert.equal(takeDraft("litellm", "pool-b", "hash-2"), "stale")
  assert.equal(takeDraft("litellm", "pool-b", "hash-1"), null)
})

test("no edits left clears the draft, and products are kept apart", () => {
  keepDraft("apim", "pool-c", null, { members: [] })
  keepDraft("litellm", "pool-c", null, { members: ["x"] })
  keepDraft("apim", "pool-c", null, null)
  assert.equal(takeDraft("apim", "pool-c", null), null)
  assert.deepEqual(takeDraft("litellm", "pool-c", null), { value: { members: ["x"] } })
})
