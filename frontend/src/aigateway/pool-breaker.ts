// The pool's resilience settings: the alias's failure rule (AI Gateway's circuit breaker) and its
// retry limit. Saving writes them to the alias. Pure, so node --test can import it.
import type { CircuitBreaker, FailureStatusCodeRange, PoolView, PoolWrite } from "./types"

/** The backend's defaults (models.py): 429, 408 and 5xx, or no answer, leave the member out
 *  for a minute; the request moves on at once. */
export const DEFAULT_BREAKER: CircuitBreaker = {
  status_code_ranges: [
    { minimum: 429, maximum: 429 },
    { minimum: 408, maximum: 408 },
    { minimum: 500, maximum: 599 },
  ],
  no_response: true,
  exclude_seconds: 60,
  same_member_retries: 0,
}
export const DEFAULT_RETRIES = 2
/** The alias's maxRetryAttempts tops out at 25. */
export const MAX_RETRIES = 25
export const MAX_EXCLUDE_SECONDS = 86_400

export type PoolSettings = { circuit_breaker: CircuitBreaker; retries: number }

export const DEFAULT_SETTINGS: PoolSettings = { circuit_breaker: DEFAULT_BREAKER, retries: DEFAULT_RETRIES }

const sameRanges = (left: FailureStatusCodeRange[], right: FailureStatusCodeRange[]) =>
  left.length === right.length
  && left.every((item, index) => item.minimum === right[index]!.minimum && item.maximum === right[index]!.maximum)

export function sameBreaker(left: CircuitBreaker | null | undefined, right: CircuitBreaker | null | undefined) {
  if (!left || !right) return left === right
  return sameRanges(left.status_code_ranges, right.status_code_ranges)
    && left.no_response === right.no_response
    && left.exclude_seconds === right.exclude_seconds
    && left.same_member_retries === right.same_member_retries
}

/** What a pool has, with the defaults where it has nothing readable (rules written elsewhere). */
export function poolSettings(pool: Pick<PoolView, "circuit_breaker" | "retries"> | null | undefined): PoolSettings {
  return {
    circuit_breaker: pool?.circuit_breaker ?? DEFAULT_BREAKER,
    retries: pool?.retries ?? DEFAULT_RETRIES,
  }
}

export function draftSettings(draft: PoolWrite): PoolSettings {
  return poolSettings({ circuit_breaker: draft.circuit_breaker ?? null, retries: draft.retries ?? null })
}

export function sameSettings(left: PoolSettings, right: PoolSettings) {
  return sameBreaker(left.circuit_breaker, right.circuit_breaker) && left.retries === right.retries
}

/** "429, 408, 500–599" for showing a rule's statuses. */
export function rangesText(ranges: FailureStatusCodeRange[]) {
  return ranges.map((item) => item.minimum === item.maximum ? String(item.minimum) : `${item.minimum}–${item.maximum}`).join(", ")
}

/** Parses "429, 408, 500-599" back into ranges; null when anything isn't a status or range. */
export function parseRanges(text: string): FailureStatusCodeRange[] | null {
  const parts = text.split(/[,，\s]+/).map((part) => part.trim()).filter(Boolean)
  const ranges: FailureStatusCodeRange[] = []
  for (const part of parts) {
    const match = /^(\d{3})(?:\s*[-–]\s*(\d{3}))?$/.exec(part)
    if (!match) return null
    const minimum = Number(match[1])
    const maximum = Number(match[2] ?? match[1])
    if (minimum < 100 || maximum > 599 || minimum > maximum) return null
    ranges.push({ minimum, maximum })
  }
  return ranges.length <= 10 ? ranges : null
}

/** Whether the rule catches an answer with this status. */
export const catchesStatus = (breaker: CircuitBreaker, status: number) =>
  breaker.status_code_ranges.some((item) => status >= item.minimum && status <= item.maximum)

/** A rule needs something to catch. */
export const breakerValid = (breaker: CircuitBreaker) =>
  breaker.status_code_ranges.length > 0 || breaker.no_response
