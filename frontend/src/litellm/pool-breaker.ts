// The pool's resilience settings: LiteLLM's cooldown (the circuit breaker), retries and the
// per-attempt timeout. Saving writes them to every member row of the pool. Pure, so node --test
// can import it.
import type { CircuitBreaker, PoolView, PoolWrite } from "./types"

/** The backend's defaults (models.py): cool a member down on its first failure, for a minute. */
export const DEFAULT_BREAKER: CircuitBreaker = { allowed_fails: 0, cooldown_seconds: 60 }
export const DEFAULT_RETRIES = 1
export const DEFAULT_TIMEOUT_SECONDS = 120
/** The longest attempt (models.py's MAX_TIMEOUT_SECONDS), the sizer's longest timeout too. */
export const MAX_TIMEOUT_SECONDS = 3600

export type PoolSettings = { circuit_breaker: CircuitBreaker; retries: number; timeout_seconds: number }

export const DEFAULT_SETTINGS: PoolSettings = {
  circuit_breaker: DEFAULT_BREAKER,
  retries: DEFAULT_RETRIES,
  timeout_seconds: DEFAULT_TIMEOUT_SECONDS,
}

export function sameBreaker(left: CircuitBreaker | null | undefined, right: CircuitBreaker | null | undefined) {
  if (!left || !right) return left === right
  return left.allowed_fails === right.allowed_fails && left.cooldown_seconds === right.cooldown_seconds
}

/** What a pool has, with the defaults where it has nothing readable (rows written elsewhere). */
export function poolSettings(pool: Pick<PoolView, "circuit_breaker" | "retries" | "timeout_seconds"> | null | undefined): PoolSettings {
  return {
    circuit_breaker: pool?.circuit_breaker ?? DEFAULT_BREAKER,
    retries: pool?.retries ?? DEFAULT_RETRIES,
    timeout_seconds: pool?.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS,
  }
}

/** The draft's settings, falling back like the backend does. */
export function draftSettings(draft: PoolWrite): PoolSettings {
  return poolSettings({
    circuit_breaker: draft.circuit_breaker ?? null,
    retries: draft.retries ?? null,
    timeout_seconds: draft.timeout_seconds ?? null,
  })
}

export function sameSettings(left: PoolSettings, right: PoolSettings) {
  return sameBreaker(left.circuit_breaker, right.circuit_breaker)
    && left.retries === right.retries
    && left.timeout_seconds === right.timeout_seconds
}

/** Failures in a minute before a member cools down: LiteLLM counts "more than allowed_fails". */
export const failuresToCool = (breaker: CircuitBreaker) => breaker.allowed_fails + 1
