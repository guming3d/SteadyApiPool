// Trying a pool from the UI: the default request and what a response means. Pure, so
// node --test can import it.
import type { ApiTestResult } from "./types"

export const DEFAULT_TEST_PATH = "chat/completions"

export function defaultTestBody(model: string) {
  return JSON.stringify({ model, messages: [{ role: "user", content: "Hello" }] }, null, 2)
}

/** The request body as an object, or null when it isn't a JSON object. */
export function parseTestBody(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text)
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null
  } catch {
    return null
  }
}

export function prettyBody(body: string) {
  try {
    return JSON.stringify(JSON.parse(body), null, 2)
  } catch {
    return body
  }
}

/**
 * Why a test call answered the way it did. LiteLLM itself answers a missing or wrong virtual
 * key with 401 (its message names the proxy token or key), and a key not allowed the model with
 * 403. A 401/403 from Azure (LiteLLM wraps it as "AzureException") means the gateway's identity
 * lacks a role on the Foundry account.
 */
export type Diagnosis =
  | "ok" | "key" | "key_scope" | "member_access" | "not_found" | "throttled" | "unavailable" | "timeout" | "other"

export function diagnose(result: Pick<ApiTestResult, "status" | "body">): Diagnosis {
  const { status } = result
  const body = result.body.toLowerCase()
  if (status >= 200 && status < 300) return "ok"
  const fromAzure = body.includes("azureexception") || body.includes("authenticationerror")
  if ((status === 401 || status === 403) && fromAzure) return "member_access"
  if (status === 401) return "key"
  if (status === 403) return "key_scope"
  if (status === 404 || (status === 400 && body.includes("invalid model name"))) return "not_found"
  if (status === 429) return "throttled"
  if (status === 503 || status === 502) return "unavailable"
  if (status === 504 || status === 408) return "timeout"
  return "other"
}

/** Which member answered (LiteLLM's model ID header is the pool's row), and the model. */
/** The member a LiteLLM row (x-litellm-model-id) stands for: rows are "<pool>--<member>";
 * anything else is shown as LiteLLM named it. */
export function memberOfRow(row: string | null) {
  return row && row.includes("--") ? row.slice(row.indexOf("--") + 2) : row
}

export function servedBy(result: Pick<ApiTestResult, "body" | "headers">) {
  let model: string | null = null
  try {
    const value = JSON.parse(result.body) as { model?: unknown }
    if (typeof value.model === "string") model = value.model
  } catch {}
  const member = memberOfRow(result.headers["x-litellm-model-id"] ?? null)
  const fallbacks = Number(result.headers["x-litellm-attempted-fallbacks"] ?? 0)
  const retries = Number(result.headers["x-litellm-attempted-retries"] ?? 0)
  return {
    model,
    member,
    region: result.headers["x-ms-region"] ?? null,
    // Attempts LiteLLM made on other members before this answer.
    rerouted: (Number.isFinite(fallbacks) ? fallbacks : 0) + (Number.isFinite(retries) ? retries : 0),
  }
}
