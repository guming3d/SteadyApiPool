// Ready-to-copy client code for an APIM API bound to a pool. The API forwards
// `<api>/chat/completions` to `<member>/openai/deployments/<name>/chat/completions`, so clients use
// a plain OpenAI client with a custom base URL. AzureOpenAI would insert /deployments/<model>/ again.
export type SnippetLanguage = "curl" | "python" | "javascript" | "powershell"
export const SNIPPET_LANGUAGES: readonly SnippetLanguage[] = ["curl", "python", "javascript", "powershell"]

// Verified against a live pool with gpt-5.6-luna; Azure OpenAI requires api-version on this path.
export const DEFAULT_API_VERSION = "2025-04-01-preview"
export const KEY_PLACEHOLDER = "<your-subscription-key>"

export type SnippetInput = {
  /** Base URL of the API, e.g. https://<apim>.azure-api.net/gpt-4o. */
  baseUrl: string
  /** Subscription key header, or null when the API needs no key. */
  keyHeader: string | null
  apiVersion: string
  /** Sent because the SDKs require it; the member URL fixes the deployment. */
  model: string
  key?: string
}

const json = (value: unknown) => JSON.stringify(value)

export function callSnippet(language: SnippetLanguage, input: SnippetInput): string {
  const key = input.key ?? KEY_PLACEHOLDER
  const base = input.baseUrl.replace(/\/+$/, "")
  const url = `${base}/chat/completions?api-version=${encodeURIComponent(input.apiVersion)}`
  const header = input.keyHeader
  const body = { model: input.model, messages: [{ role: "user", content: "Hello" }] }

  switch (language) {
    case "curl":
      return [
        `curl "${url}" \\`,
        ...header ? [`  -H "${header}: ${key}" \\`] : [],
        `  -H "Content-Type: application/json" \\`,
        `  -d '${json(body)}'`,
      ].join("\n")
    case "python":
      return [
        "from openai import OpenAI",
        "",
        "client = OpenAI(",
        `    base_url=${json(base)},`,
        `    api_key="unused",  # The SDK requires a value; APIM sends its own token to Azure OpenAI`,
        ...header ? [`    default_headers={${json(header)}: ${json(key)}},`] : [],
        `    default_query={"api-version": ${json(input.apiVersion)}},`,
        ")",
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
        "const client = new OpenAI({",
        `  baseURL: ${json(base)},`,
        `  apiKey: "unused", // The SDK requires a value; APIM sends its own token to Azure OpenAI`,
        ...header ? [`  defaultHeaders: { ${json(header)}: ${json(key)} },`] : [],
        `  defaultQuery: { "api-version": ${json(input.apiVersion)} },`,
        "})",
        "",
        "const response = await client.chat.completions.create({",
        `  model: ${json(input.model)},`,
        `  messages: [{ role: "user", content: "Hello" }],`,
        "})",
        "console.log(response.choices[0].message.content)",
      ].join("\n")
    case "powershell":
      return [
        ...header ? [`$headers = @{ "${header}" = "${key}" }`] : ["$headers = @{}"],
        `$body = @{ model = "${input.model}"; messages = @(@{ role = "user"; content = "Hello" }) } | ConvertTo-Json -Depth 5`,
        `$response = Invoke-RestMethod -Method Post -Uri "${url}" -Headers $headers -ContentType "application/json" -Body $body`,
        "$response.choices[0].message.content",
      ].join("\n")
  }
}
