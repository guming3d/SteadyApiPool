// Trying a pool API from the UI: the default request and what a response means. Pure, so
// node --test can import it.
import type { ApiTestResult } from "../types"

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
 * Why a test call answered the way it did. APIM itself answers a missing or wrong key with 401
 * and a message about the subscription key; a 401/403 otherwise comes from the member, usually
 * because APIM's identity lacks a role on the Foundry account.
 */
export type Diagnosis =
  | "ok" | "key" | "member_access" | "not_found" | "throttled" | "no_identity" | "unavailable" | "timeout" | "other"

export function diagnose(result: Pick<ApiTestResult, "status" | "body">): Diagnosis {
  const { status } = result
  const body = result.body.toLowerCase()
  if (status >= 200 && status < 300) return "ok"
  if (status === 401 && body.includes("subscription key")) return "key"
  if (status === 401 || status === 403) return "member_access"
  if (status === 404) return "not_found"
  if (status === 429) return "throttled"
  if (status === 500) return "no_identity"
  if (status === 503) return "unavailable"
  if (status === 504 || status === 408) return "timeout"
  return "other"
}

/** The answering model and region, when the response says so. */
export function servedBy(result: Pick<ApiTestResult, "body" | "headers">) {
  let model: string | null = null
  try {
    const value = JSON.parse(result.body) as { model?: unknown }
    if (typeof value.model === "string") model = value.model
  } catch {}
  return { model, region: result.headers["x-ms-region"] ?? null }
}
