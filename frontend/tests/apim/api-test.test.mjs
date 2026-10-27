import assert from "node:assert/strict"
import test from "node:test"

import { defaultTestBody, diagnose, parseTestBody, prettyBody, servedBy } from "../../src/apim/api-test.ts"

test("the default body is a chat request for the pool's model", () => {
  assert.deepEqual(JSON.parse(defaultTestBody("gpt-4o")), { model: "gpt-4o", messages: [{ role: "user", content: "Hello" }] })
})

test("only JSON objects are sent", () => {
  assert.deepEqual(parseTestBody('{"a": 1}'), { a: 1 })
  assert.equal(parseTestBody("[1]"), null)
  assert.equal(parseTestBody("null"), null)
  assert.equal(parseTestBody("{"), null)
})

test("APIM's own 401 is about the key, a member's is about access", () => {
  const apim = { status: 401, body: '{"statusCode":401,"message":"Access denied due to invalid subscription key."}' }
  const member = { status: 401, body: '{"error":{"code":"PermissionDenied","message":"The principal lacks the required data action"}}' }
  assert.equal(diagnose(apim), "key")
  assert.equal(diagnose(member), "member_access")
  assert.equal(diagnose({ status: 403, body: "" }), "member_access")
})

test("other statuses", () => {
  assert.equal(diagnose({ status: 200, body: "{}" }), "ok")
  assert.equal(diagnose({ status: 404, body: "" }), "not_found")
  assert.equal(diagnose({ status: 429, body: "" }), "throttled")
  assert.equal(diagnose({ status: 500, body: "" }), "no_identity")
  assert.equal(diagnose({ status: 503, body: "" }), "unavailable")
  assert.equal(diagnose({ status: 504, body: "" }), "timeout")
  assert.equal(diagnose({ status: 418, body: "" }), "other")
})

test("served by reads the model and region when present", () => {
  assert.deepEqual(servedBy({ body: '{"model":"gpt-4o-2024"}', headers: { "x-ms-region": "East US 2" } }), { model: "gpt-4o-2024", region: "East US 2" })
  assert.deepEqual(servedBy({ body: "not json", headers: {} }), { model: null, region: null })
  assert.equal(prettyBody('{"a":1}'), '{\n  "a": 1\n}')
  assert.equal(prettyBody("plain"), "plain")
})
