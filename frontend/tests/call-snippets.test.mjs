import assert from "node:assert/strict"
import test from "node:test"

import { callSnippet, KEY_PLACEHOLDER, SNIPPET_LANGUAGES } from "../src/call-snippets.ts"

const input = { baseUrl: "https://gw.example.azurecontainerapps.io/v1/", model: "gpt6" }

test("every snippet calls the gateway's chat completions with a bearer key and the pool's name", () => {
  for (const language of SNIPPET_LANGUAGES) {
    const code = callSnippet(language, input)
    assert.match(code, /https:\/\/gw\.example\.azurecontainerapps\.io\/v1/, language)
    assert.doesNotMatch(code, /v1\/\//, `${language}: trailing slash trimmed`)
    assert.ok(code.includes(KEY_PLACEHOLDER), language)
    assert.ok(code.includes("gpt6"), language)
    // The gateway speaks plain OpenAI: no api-version, no AzureOpenAI client.
    assert.doesNotMatch(code, /api-version|AzureOpenAI|Ocp-Apim/, language)
  }
  assert.ok(callSnippet("curl", input).includes(`"https://gw.example.azurecontainerapps.io/v1/chat/completions"`))
  assert.ok(callSnippet("curl", input).includes(`Authorization: Bearer ${KEY_PLACEHOLDER}`))
})

test("a real key can be substituted for testing", () => {
  assert.ok(callSnippet("python", { ...input, key: "sk-abc123" }).includes(`api_key="sk-abc123"`))
  assert.ok(callSnippet("javascript", { ...input, key: "sk-abc123" }).includes(`apiKey: "sk-abc123"`))
})
