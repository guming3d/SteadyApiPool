import assert from "node:assert/strict"
import test from "node:test"

import { callSnippet, KEY_PLACEHOLDER, SNIPPET_LANGUAGES } from "../../src/apim/call-snippets.ts"

const input = {
  baseUrl: "https://apim.azure-api.net/gpt-5.6-luna/",
  keyHeader: "Ocp-Apim-Subscription-Key",
  apiVersion: "2025-04-01-preview",
  model: "gpt-5.6-luna",
}

test("every snippet calls the API's chat completions with the key and api-version", () => {
  for (const language of SNIPPET_LANGUAGES) {
    const code = callSnippet(language, input)
    assert.match(code, /https:\/\/apim\.azure-api\.net\/gpt-5\.6-luna/, language)
    assert.doesNotMatch(code, /gpt-5\.6-luna\/\//, `${language}: trailing slash trimmed`)
    assert.ok(code.includes("Ocp-Apim-Subscription-Key"), language)
    assert.ok(code.includes(KEY_PLACEHOLDER), language)
    assert.ok(code.includes("2025-04-01-preview"), language)
    // AzureOpenAI would add /openai/deployments/<model>/ a second time.
    assert.doesNotMatch(code, /AzureOpenAI/, language)
  }
  assert.ok(callSnippet("curl", input).includes(
    `"https://apim.azure-api.net/gpt-5.6-luna/chat/completions?api-version=2025-04-01-preview"`))
})

test("snippets drop the key header when the API needs no subscription", () => {
  for (const language of SNIPPET_LANGUAGES) {
    const code = callSnippet(language, { ...input, keyHeader: null })
    assert.ok(!code.includes("Ocp-Apim-Subscription-Key"), language)
    assert.ok(!code.includes(KEY_PLACEHOLDER), language)
  }
})

test("a real key can be substituted for testing", () => {
  assert.ok(callSnippet("python", { ...input, key: "abc123" }).includes(`{"Ocp-Apim-Subscription-Key": "abc123"}`))
})
