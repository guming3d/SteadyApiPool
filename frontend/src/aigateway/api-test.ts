// Trying a pool from the UI: the default request and what a response means. Pure, so
// node --test can import it.
import type { ApiTestResult } from "../types"
import type { TestPath } from "./types"

export const TEST_PATHS: readonly TestPath[] = ["chat/completions", "responses", "embeddings"]
export const DEFAULT_TEST_PATH: TestPath = "chat/completions"

export function defaultTestBody(model: string, path: TestPath = DEFAULT_TEST_PATH) {
  const body = path === "responses"
    ? { model, input: "Hello" }
    : path === "embeddings"
      ? { model, input: "Hello" }
      : { model, messages: [{ role: "user", content: "Hello" }] }
  return JSON.stringify(body, null, 2)
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
 * Why a test call answered the way it did. The gateway answers a missing, wrong or suspended key
 * with 401 ("Missing or invalid Entra ID token" in the same words); an unknown model name with
 * 404 (LlmAliasNotFound). A 401/403 from Foundry behind it means the gateway's identity lacks
 * Foundry User on the account; an empty 503 that the failure rule matched but no retry was left.
 */
export type Diagnosis =
  | "ok" | "key" | "member_access" | "not_found" | "throttled" | "unavailable" | "no_retry" | "timeout" | "other"

export function diagnose(result: Pick<ApiTestResult, "status" | "body">): Diagnosis {
  const { status } = result
  const body = result.body.toLowerCase()
  if (status >= 200 && status < 300) return "ok"
  if (status === 401 && (body.includes("entra id token") || body.includes("data.all") || !body.trim())) return "key"
  if (status === 401 || status === 403) return "member_access"
  if (status === 404) return "not_found"
  if (status === 429) return "throttled"
  if (status === 503 && !body.trim()) return "no_retry"
  if (status === 503 || status === 502) return "unavailable"
  if (status === 504 || status === 408) return "timeout"
  return "other"
}

/** Which backend model answered: Foundry members say so in x-ms-served-model. */
export function servedBy(result: Pick<ApiTestResult, "body" | "headers">) {
  let model: string | null = null
  try {
    const value = JSON.parse(result.body) as { model?: unknown }
    if (typeof value.model === "string") model = value.model
  } catch {}
  return {
    model,
    member: result.headers["x-ms-served-model"] ?? null,
    region: result.headers["x-ms-region"] ?? null,
  }
}

/** The member a load test sample names (x-ms-served-model), as the gateway named it. */
export const memberOfServed = (served: string | null) => served
