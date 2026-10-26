import assert from "node:assert/strict"
import test from "node:test"

import { progressRows } from "../src/setup-progress.ts"

test("before the report starts the request is still connecting", () => {
  assert.deepEqual(progressRows(undefined), [{ key: "connecting", running: true }])
  assert.deepEqual(progressRows({ running: false, steps: [] }), [{ key: "connecting", running: true }])
})

test("steps keep their order, subject and counts", () => {
  const rows = progressRows({
    running: true,
    steps: [
      { kind: "sign_in", state: "done", subject: null, done: null, total: null },
      { kind: "gateway_target", state: "done", subject: "gw-prod", done: null, total: null },
      { kind: "foundry_access", state: "running", subject: null, done: 3, total: 7 },
    ],
  })
  assert.deepEqual(rows, [
    { key: "sign_in", running: false },
    { key: "gateway_target", running: false, subject: "gw-prod" },
    { key: "foundry_access", running: true, count: { done: 3, total: 7 } },
  ])
})

test("counts never pass their total, and an empty total shows none", () => {
  const [over, none] = progressRows({
    running: true,
    steps: [
      { kind: "discovery", state: "done", done: 4, total: 2 },
      { kind: "discovery", state: "running", done: 0, total: 0 },
    ],
  })
  assert.deepEqual(over.count, { done: 2, total: 2 })
  assert.equal(none.count, undefined)
})
