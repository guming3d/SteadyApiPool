// The pool's circuit breaker. APIM keeps a breaker on each member backend; this app treats it as
// one pool setting and, on publish, writes it to every member it manages (the backend skips
// members whose rule already matches). Pure, so node --test can import it; tui/pool_tui/logic.py
// ports it.
import type { BackendView, CircuitBreaker } from "./types"

/** The app's platform breaker; the same as the backend's CircuitBreaker defaults. */
export const DEFAULT_BREAKER: CircuitBreaker = {
  failure_count: 1,
  interval_seconds: 60,
  trip_duration_seconds: 60,
  accept_retry_after: true,
  status_code_ranges: [
    { minimum: 429, maximum: 429 },
    { minimum: 408, maximum: 408 },
    { minimum: 500, maximum: 599 },
  ],
  error_reasons: ["BackendConnectionFailure", "Timeout"],
}

/** Publishing writes the pool's rule to this member; backends this app didn't create are read-only. */
export const followsPool = (backend: BackendView | undefined): boolean =>
  backend !== undefined && backend.managed && !backend.protected

/** Field by field, in order, as the backend compares them before rewriting a member. */
export function sameBreaker(left: CircuitBreaker | null, right: CircuitBreaker | null) {
  if (!left || !right) return left === right
  return left.failure_count === right.failure_count
    && left.interval_seconds === right.interval_seconds
    && left.trip_duration_seconds === right.trip_duration_seconds
    && left.accept_retry_after === right.accept_retry_after
    && JSON.stringify(left.status_code_ranges) === JSON.stringify(right.status_code_ranges)
    && JSON.stringify(left.error_reasons) === JSON.stringify(right.error_reasons)
}

const lookup = (backendById: Map<string, BackendView>, backendId: string) => backendById.get(backendId.toLowerCase())

/**
 * The rule a pool's editor starts from: the one most of its managed members share (a tie goes
 * to the earlier member, i.e. the primary), else the default. ``uniform`` is false when those
 * members disagree or some have no readable rule, so publishing would change some of them.
 */
export function poolBreaker(memberIds: string[], backendById: Map<string, BackendView>): { rule: CircuitBreaker; uniform: boolean } {
  const rules = memberIds.flatMap((id) => {
    const backend = lookup(backendById, id)
    return backend && followsPool(backend) ? [backend.circuit_breaker] : []
  })
  const counts: Array<{ rule: CircuitBreaker; count: number }> = []
  for (const rule of rules) {
    if (!rule) continue
    const seen = counts.find((entry) => sameBreaker(entry.rule, rule))
    if (seen) seen.count += 1
    else counts.push({ rule, count: 1 })
  }
  const best = counts.reduce<{ rule: CircuitBreaker; count: number } | null>(
    (top, entry) => !top || entry.count > top.count ? entry : top, null)
  const rule = best?.rule ?? DEFAULT_BREAKER
  return { rule, uniform: rules.every((candidate) => sameBreaker(candidate, rule)) }
}

/** Managed members whose rule publishing ``rule`` would rewrite. */
export function membersToUpdate(memberIds: string[], backendById: Map<string, BackendView>, rule: CircuitBreaker) {
  return memberIds.filter((id) => {
    const backend = lookup(backendById, id)
    return backend !== undefined && followsPool(backend) && !sameBreaker(backend.circuit_breaker, rule)
  })
}

/** Members publishing can't update: they keep whatever rule they have (or none). */
export function readOnlyMembers(memberIds: string[], backendById: Map<string, BackendView>) {
  return memberIds.filter((id) => {
    const backend = lookup(backendById, id)
    return backend !== undefined && !followsPool(backend)
  })
}

/** The backends as they will be once the draft is published, for the preview and the checks. */
export function withPoolBreaker(backendById: Map<string, BackendView>, memberIds: string[], rule: CircuitBreaker) {
  const next = new Map(backendById)
  for (const id of memberIds) {
    const backend = lookup(backendById, id)
    if (backend && followsPool(backend)) next.set(id.toLowerCase(), { ...backend, circuit_breaker: rule, has_circuit_breaker: true })
  }
  return next
}
