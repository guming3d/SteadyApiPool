// Ready-to-copy client code for a pool. AI Gateway speaks the OpenAI v1 API at
// <host>/default/models/openai/v1, and the pool's alias is the model name, so clients use a plain
// OpenAI client with a custom base URL and a runtime API key (or the api-key header). Pure, so
// node --test can import it.
export type SnippetLanguage = "curl" | "python" | "javascript" | "powershell"
export const SNIPPET_LANGUAGES: readonly SnippetLanguage[] = ["curl", "python", "javascript", "powershell"]

export const KEY_PLACEHOLDER = "<your-ai-gateway-key>"

export type SnippetInput = {
  /** https://<host>/default/models/openai/v1. */
  baseUrl: string
  /** The pool's model name (its alias). */
  model: string
  key?: string
}

const json = (value: unknown) => JSON.stringify(value)

export function callSnippet(language: SnippetLanguage, input: SnippetInput): string {
  const key = input.key ?? KEY_PLACEHOLDER
  const base = input.baseUrl.replace(/\/+$/, "")
  const url = `${base}/chat/completions`
  const body = { model: input.model, messages: [{ role: "user", content: "Hello" }] }

  switch (language) {
    case "curl":
      return [
        `curl "${url}" \\`,
        `  -H "Authorization: Bearer ${key}" \\`,
        `  -H "Content-Type: application/json" \\`,
        `  -d '${json(body)}'`,
      ].join("\n")
    case "python":
      return [
        "from openai import OpenAI",
        "",
        `client = OpenAI(base_url=${json(base)}, api_key=${json(key)})`,
        "",
        "response = client.chat.completions.create(",
        `    model=${json(input.model)},`,
        `    messages=[{"role": "user", "content": "Hello"}],`,
        ")",
        "print(response.choices[0].message.content)",
      ].join("\n")
    case "javascript":
      return [
        `import OpenAI from "openai"`,
        "",
        `const client = new OpenAI({ baseURL: ${json(base)}, apiKey: ${json(key)} })`,
        "",
        "const response = await client.chat.completions.create({",
        `  model: ${json(input.model)},`,
        `  messages: [{ role: "user", content: "Hello" }],`,
        "})",
        "console.log(response.choices[0].message.content)",
      ].join("\n")
    case "powershell":
      return [
        `$headers = @{ Authorization = "Bearer ${key}" }`,
        `$body = @{ model = "${input.model}"; messages = @(@{ role = "user"; content = "Hello" }) } | ConvertTo-Json -Depth 5`,
        `$response = Invoke-RestMethod -Method Post -Uri "${url}" -Headers $headers -ContentType "application/json" -Body $body`,
        "$response.choices[0].message.content",
      ].join("\n")
  }
}
