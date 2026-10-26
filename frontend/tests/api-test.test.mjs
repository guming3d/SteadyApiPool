import assert from "node:assert/strict"
import test from "node:test"

import { defaultTestBody, diagnose, parseTestBody, prettyBody, servedBy } from "../src/api-test.ts"

test("the default body is a chat request for the pool's model name", () => {
  assert.deepEqual(JSON.parse(defaultTestBody("gpt6")), { model: "gpt6", messages: [{ role: "user", content: "Hello" }] })
})

test("only JSON objects are sent", () => {
  assert.deepEqual(parseTestBody('{"a": 1}'), { a: 1 })
  assert.equal(parseTestBody("[1]"), null)
  assert.equal(parseTestBody("null"), null)
  assert.equal(parseTestBody("{"), null)
})

test("the gateway's own 401/403 is about the key, Azure's is about the gateway's identity", () => {
  const badKey = { status: 401, body: '{"error":{"message":"Authentication Error, Invalid proxy server token passed.","code":"401"}}' }
  const scope = { status: 403, body: "{\"error\":{\"message\":\"The requested model 'x' is not available for this API key\",\"type\":\"key_model_access_denied\"}}" }
  const azure = { status: 401, body: '{"error":{"message":"litellm.AuthenticationError: AzureException AuthenticationError - PermissionDenied"}}' }
  assert.equal(diagnose(badKey), "key")
  assert.equal(diagnose(scope), "key_scope")
  assert.equal(diagnose(azure), "member_access")
  assert.equal(diagnose({ status: 403, body: "AzureException - The principal lacks the data action" }), "member_access")
})

test("other statuses", () => {
  assert.equal(diagnose({ status: 200, body: "{}" }), "ok")
  assert.equal(diagnose({ status: 404, body: "" }), "not_found")
  assert.equal(diagnose({ status: 400, body: "Invalid model name passed in model=x" }), "not_found")
  assert.equal(diagnose({ status: 429, body: "" }), "throttled")
  assert.equal(diagnose({ status: 503, body: "" }), "unavailable")
  assert.equal(diagnose({ status: 502, body: "" }), "unavailable")
  assert.equal(diagnose({ status: 504, body: "" }), "timeout")
  assert.equal(diagnose({ status: 418, body: "" }), "other")
})

test("served by names the member LiteLLM routed to, and what it tried first", () => {
  const result = servedBy({
    body: '{"model":"gpt-6-2026"}',
    headers: { "x-litellm-model-id": "gpt6--pool-aoai-west-gpt6", "x-litellm-attempted-fallbacks": "1", "x-ms-region": "West US 3" },
  })
  assert.deepEqual(result, { model: "gpt-6-2026", member: "pool-aoai-west-gpt6", region: "West US 3", rerouted: 1 })
  assert.deepEqual(servedBy({ body: "not json", headers: {} }), { model: null, member: null, region: null, rerouted: 0 })
  // Rows LiteLLM named itself are shown as they are.
  assert.equal(servedBy({ body: "{}", headers: { "x-litellm-model-id": "abc123" } }).member, "abc123")
  assert.equal(prettyBody('{"a":1}'), '{\n  "a": 1\n}')
  assert.equal(prettyBody("plain"), "plain")
})
