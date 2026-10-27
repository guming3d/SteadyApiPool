import assert from "node:assert/strict"
import test from "node:test"

import {
  DEFAULT_BREAKER,
  DEFAULT_SETTINGS,
  draftSettings,
  failuresToCool,
  poolSettings,
  sameBreaker,
  sameSettings,
} from "../../src/litellm/pool-breaker.ts"

test("the defaults match the backend: cool down on the first failure, for a minute", () => {
  assert.deepEqual(DEFAULT_BREAKER, { allowed_fails: 0, cooldown_seconds: 60 })
  assert.deepEqual(DEFAULT_SETTINGS, { circuit_breaker: DEFAULT_BREAKER, retries: 1, timeout_seconds: 120 })
  assert.equal(failuresToCool(DEFAULT_BREAKER), 1)
  assert.equal(failuresToCool({ allowed_fails: 2, cooldown_seconds: 30 }), 3)
})

test("a pool without readable settings starts from the defaults", () => {
  assert.deepEqual(poolSettings(null), DEFAULT_SETTINGS)
  assert.deepEqual(poolSettings({ circuit_breaker: null, retries: 3, timeout_seconds: null }), { ...DEFAULT_SETTINGS, retries: 3 })
  assert.deepEqual(draftSettings({ members: [], timeout_seconds: 30 }), { ...DEFAULT_SETTINGS, timeout_seconds: 30 })
})

test("settings compare field by field", () => {
  assert.equal(sameBreaker(DEFAULT_BREAKER, { ...DEFAULT_BREAKER }), true)
  assert.equal(sameBreaker(DEFAULT_BREAKER, { allowed_fails: 0, cooldown_seconds: 30 }), false)
  assert.equal(sameBreaker(null, null), true)
  assert.equal(sameBreaker(null, DEFAULT_BREAKER), false)
  assert.equal(sameSettings(DEFAULT_SETTINGS, { ...DEFAULT_SETTINGS }), true)
  assert.equal(sameSettings(DEFAULT_SETTINGS, { ...DEFAULT_SETTINGS, retries: 0 }), false)
})
