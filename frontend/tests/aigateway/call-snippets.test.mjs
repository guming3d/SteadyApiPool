import assert from "node:assert/strict"
import test from "node:test"

import { defaultTestBody, diagnose, parseTestBody, servedBy } from "../../src/aigateway/api-test.ts"
import { callSnippet, KEY_PLACEHOLDER, SNIPPET_LANGUAGES } from "../../src/aigateway/call-snippets.ts"
import { confirmPlan, nameMatches, planIsEmpty } from "../../src/aigateway/removal.ts"
import { GATEWAY_NAME_PATTERN, suggestGatewayName } from "../../src/aigateway/pool-ids.ts"

const BASE = "https://aigw.eastus2-01.ai.gateway.azure.com/default/models/openai/v1"

test("every snippet calls the OpenAI v1 surface with the alias as the model", () => {
  for (const language of SNIPPET_LANGUAGES) {
    const code = callSnippet(language, { baseUrl: `${BASE}/`, model: "gpt6" })
    assert.ok(code.includes(KEY_PLACEHOLDER), language)
    assert.ok(code.includes("gpt6"), language)
    assert.ok(!code.includes("api-version"), language)
  }
  assert.ok(callSnippet("curl", { baseUrl: BASE, model: "gpt6" }).includes(`"${BASE}/chat/completions"`))
  assert.ok(callSnippet("python", { baseUrl: BASE, model: "gpt6", key: "k" }).includes(`base_url="${BASE}", api_key="k"`))
})

test("the test body follows the path, and answers are explained", () => {
  assert.deepEqual(parseTestBody(defaultTestBody("gpt6")), { model: "gpt6", messages: [{ role: "user", content: "Hello" }] })
  assert.deepEqual(parseTestBody(defaultTestBody("gpt6", "responses")), { model: "gpt6", input: "Hello" })
  assert.equal(parseTestBody("[1]"), null)
  assert.equal(diagnose({ status: 200, body: "" }), "ok")
  assert.equal(diagnose({ status: 401, body: "Unauthorized. Missing or invalid Entra ID token, or missing Data.All scope." }), "key")
  assert.equal(diagnose({ status: 401, body: '{"error":{"code":"PermissionDenied"}}' }), "member_access")
  assert.equal(diagnose({ status: 503, body: "" }), "no_retry")
  assert.equal(diagnose({ status: 503, body: "busy" }), "unavailable")
  assert.equal(diagnose({ status: 404, body: "LlmAliasNotFound" }), "not_found")
  assert.deepEqual(
    servedBy({ body: '{"model":"gpt-6-2026"}', headers: { "x-ms-served-model": "gpt-6-luna", "x-ms-region": "East US 2" } }),
    { model: "gpt-6-2026", member: "gpt-6-luna", region: "East US 2" },
  )
})

test("removal confirms exactly the plan shown", () => {
  const plan = { pools: ["gpt6"], delete_members: ["m1"], delete_keys: ["pool-test"], kept_pools: ["theirs"], blocked: [] }
  assert.deepEqual(confirmPlan(plan, '"3"'), { pools: ["gpt6"], delete_keys: ["pool-test"], etag: '"3"' })
  assert.deepEqual(confirmPlan(plan), { pools: ["gpt6"], delete_keys: ["pool-test"] })
  assert.equal(planIsEmpty({ ...plan, pools: [], delete_members: [], delete_keys: [] }), true)
  assert.equal(planIsEmpty({ ...plan, pools: [], delete_keys: [] }), false)
  assert.equal(nameMatches(" AIGW ", "aigw"), true)
})

test("suggested gateway names are valid", () => {
  for (let index = 0; index < 50; index++) assert.ok(GATEWAY_NAME_PATTERN.test(suggestGatewayName()))
  assert.ok(GATEWAY_NAME_PATTERN.test(suggestGatewayName(() => 0)))
  assert.equal(GATEWAY_NAME_PATTERN.test("-aigw"), false)
  assert.equal(GATEWAY_NAME_PATTERN.test("a".repeat(25)), false)
})
