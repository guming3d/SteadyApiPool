import assert from "node:assert/strict"
import test from "node:test"

import {
  breakerValid,
  catchesStatus,
  DEFAULT_BREAKER,
  DEFAULT_SETTINGS,
  draftSettings,
  parseRanges,
  poolSettings,
  rangesText,
  sameBreaker,
  sameSettings,
} from "../../src/aigateway/pool-breaker.ts"

test("the defaults match the backend: 429, 408, 5xx or no answer excludes for a minute, 2 retries", () => {
  assert.deepEqual(DEFAULT_BREAKER, {
    status_code_ranges: [{ minimum: 429, maximum: 429 }, { minimum: 408, maximum: 408 }, { minimum: 500, maximum: 599 }],
    no_response: true,
    exclude_seconds: 60,
    same_member_retries: 0,
  })
  assert.deepEqual(DEFAULT_SETTINGS, { circuit_breaker: DEFAULT_BREAKER, retries: 2 })
})

test("a pool without readable settings starts from the defaults", () => {
  assert.deepEqual(poolSettings(null), DEFAULT_SETTINGS)
  assert.deepEqual(poolSettings({ circuit_breaker: null, retries: 5 }), { ...DEFAULT_SETTINGS, retries: 5 })
  assert.deepEqual(draftSettings({ members: [], retries: 0 }), { ...DEFAULT_SETTINGS, retries: 0 })
})

test("settings compare field by field, ranges in order", () => {
  assert.equal(sameBreaker(DEFAULT_BREAKER, structuredClone(DEFAULT_BREAKER)), true)
  assert.equal(sameBreaker(DEFAULT_BREAKER, { ...DEFAULT_BREAKER, exclude_seconds: 30 }), false)
  assert.equal(sameBreaker(DEFAULT_BREAKER, { ...DEFAULT_BREAKER, status_code_ranges: DEFAULT_BREAKER.status_code_ranges.slice(1) }), false)
  assert.equal(sameBreaker(null, null), true)
  assert.equal(sameSettings(DEFAULT_SETTINGS, { ...DEFAULT_SETTINGS, retries: 3 }), false)
})

test("status ranges read and write as the editor shows them", () => {
  assert.equal(rangesText(DEFAULT_BREAKER.status_code_ranges), "429, 408, 500–599")
  assert.deepEqual(parseRanges("429, 408, 500-599"), DEFAULT_BREAKER.status_code_ranges)
  assert.deepEqual(parseRanges("500–599"), [{ minimum: 500, maximum: 599 }])
  assert.deepEqual(parseRanges(""), [])
  assert.equal(parseRanges("42"), null)
  assert.equal(parseRanges("599-500"), null)
  assert.equal(parseRanges("700"), null)
  assert.equal(parseRanges(Array.from({ length: 11 }, (_, i) => 500 + i).join(",")), null)
})

test("a rule catches its statuses and needs something to catch", () => {
  assert.equal(catchesStatus(DEFAULT_BREAKER, 503), true)
  assert.equal(catchesStatus(DEFAULT_BREAKER, 404), false)
  assert.equal(breakerValid({ ...DEFAULT_BREAKER, status_code_ranges: [] }), true)
  assert.equal(breakerValid({ ...DEFAULT_BREAKER, status_code_ranges: [], no_response: false }), false)
})
