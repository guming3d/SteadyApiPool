// What APIM does with a pool configuration, as plain data for the routing preview.
// Behaviour follows https://learn.microsoft.com/azure/api-management/backends: the gateway
// sends traffic to the highest priority group (lowest number) that still has a member whose
// breaker has not tripped, splits it by weight inside that group, and returns 503 when no
// member is left. It never health-checks: a breaker only trips when real requests fail.
import type { CircuitBreaker, PoolMember } from "./types"

export type FailureKind = "throttle" | "outage"
export type Phase = "normal" | "failing" | "tripped" | "recovered"
export const PHASES: readonly Phase[] = ["normal", "failing", "tripped", "recovered"]

export type SimMember = PoolMember & { breaker: CircuitBreaker | null }
export type SimRetry = { attempts: number; interval: number; firstFast: boolean }

export type SimInput = {
  members: SimMember[]
  /** 429 retry of the bound API's policy; null when unbound or retry is off. */
  retry: SimRetry | null
  bound: boolean
  sessionAffinity: boolean
  failing: ReadonlySet<string>
  kind: FailureKind
}

export type MemberState = "serving" | "standby" | "tripped"
export type MemberResult = {
  backend_id: string
  priority: number
  state: MemberState
  /** Percentage of new requests this member receives. */
  share: number
  /** Returning errors in this phase. */
  failing: boolean
}
/** ok: clients see no errors. degraded: some requests fail. down: APIM answers 503. */
export type Outcome = "ok" | "degraded" | "down"
export type SimResult = { members: MemberResult[]; outcome: Outcome; servingPriority: number | null }

/** Whether a member's breaker counts this kind of failure. */
export function breakerCatches(breaker: CircuitBreaker | null, kind: FailureKind) {
  if (!breaker) return false
  if (kind === "throttle") {
    return breaker.status_code_ranges.some((range) => range.minimum <= 429 && 429 <= range.maximum)
  }
  return breaker.status_code_ranges.some((range) => range.minimum <= 599 && range.maximum >= 500)
    || breaker.error_reasons.some((reason) => reason !== "ClientConnectionFailure")
}

/** Shares of new requests when the members in `out` are unavailable. */
export function route(members: PoolMember[], out: ReadonlySet<string> = new Set()) {
  const shares = new Map<string, number>()
  const available = members.filter((member) => !out.has(member.backend_id))
  if (!available.length) return { shares, priority: null }
  const priority = Math.min(...available.map((member) => member.priority))
  const group = available.filter((member) => member.priority === priority)
  const total = group.reduce((sum, member) => sum + Math.max(0, member.weight), 0)
  for (const member of group) {
    shares.set(member.backend_id, total > 0 ? Math.max(0, member.weight) / total * 100 : 100 / group.length)
  }
  return { shares, priority }
}

function trippedMembers(input: SimInput) {
  // A breaker trips only once failing requests reach its member, so follow the traffic:
  // tripping a primary can send traffic to a standby that is failing too.
  const tripped = new Set<string>()
  for (let round = 0; round <= input.members.length; round++) {
    const { shares } = route(input.members, tripped)
    const next = input.members.filter((member) =>
      input.failing.has(member.backend_id)
      && (shares.get(member.backend_id) ?? 0) > 0
      && breakerCatches(member.breaker, input.kind))
    if (!next.length) break
    for (const member of next) tripped.add(member.backend_id)
  }
  return tripped
}

export function simulate(input: SimInput, phase: Phase): SimResult {
  const failingNow = phase === "failing" || phase === "tripped"
  const tripped = phase === "tripped" ? trippedMembers(input) : new Set<string>()
  const { shares, priority } = route(input.members, tripped)
  const members = input.members.map((member): MemberResult => {
    const share = shares.get(member.backend_id) ?? 0
    return {
      backend_id: member.backend_id,
      priority: member.priority,
      state: tripped.has(member.backend_id) ? "tripped" : share > 0 ? "serving" : "standby",
      share,
      failing: failingNow && input.failing.has(member.backend_id) && !tripped.has(member.backend_id),
    }
  })
  return { members, outcome: outcome(input, phase, members, priority), servingPriority: priority }
}

function outcome(input: SimInput, phase: Phase, members: MemberResult[], priority: number | null): Outcome {
  if (priority === null) return "down"
  const hit = members.filter((member) => member.failing && member.share > 0)
  if (!hit.length) return "ok"
  if (phase !== "failing") return "degraded"
  // A single 429 trips a count-1 breaker, and the retry is routed past the tripped member.
  const absorbed = input.kind === "throttle" && input.retry !== null
    && hit.every((result) => {
      const breaker = input.members.find((member) => member.backend_id === result.backend_id)?.breaker ?? null
      return breakerCatches(breaker, "throttle") && breaker?.failure_count === 1
    })
    && simulate(input, "tripped").servingPriority !== null
  return absorbed ? "ok" : "degraded"
}

export type Tone = "info" | "ok" | "warn" | "error" | "trip"
export type Line = { key: NarrationKey; tone: Tone; values?: Record<string, string | number | string[]> }

/** Every message key the narration can produce; each must exist in i18n.tsx. */
export type NarrationKey =
  | "simNotBound" | "simServingOne" | "simServingSplit" | "simStandby" | "simAffinity"
  | "simNoBreaker" | "simHint" | "simFailing429" | "simFailing5xx" | "simTripRule"
  | "simTripRuleRetryAfter" | "simNoBreakerMember" | "simBreakerMisses" | "simFailingIdle"
  | "simRetry429Fast" | "simRetry429" | "simNoRetry429" | "simOutageNoRetry" | "simTripped"
  | "simAllDown" | "simFailoverTo" | "simShiftTo" | "simStillFailing" | "simClientOk"
  | "simNothingTripped" | "simRecovered" | "simRecoveredRetryAfter" | "simRetrip"

const percent = (value: number) => `${Math.round(value)}%`

export function narrate(input: SimInput, phase: Phase, label: (backendId: string) => string): Line[] {
  const result = simulate(input, phase)
  const byId = new Map(input.members.map((member) => [member.backend_id, member]))
  const names = (items: MemberResult[]) => items.map((item) => label(item.backend_id))
  const serving = result.members.filter((member) => member.state === "serving")
  const lines: Line[] = []

  if (phase === "normal") {
    if (!input.bound) lines.push({ key: "simNotBound", tone: "warn" })
    if (serving.length && result.servingPriority !== null) {
      lines.push(serving.length === 1
        ? { key: "simServingOne", tone: "ok", values: { priority: result.servingPriority + 1, names: names(serving) } }
        : {
          key: "simServingSplit",
          tone: "ok",
          values: {
            priority: result.servingPriority + 1,
            names: serving.map((item) => `${label(item.backend_id)} ${percent(item.share)}`),
          },
        })
    }
    const standby = result.members.filter((member) => member.state === "standby")
    if (standby.length) lines.push({ key: "simStandby", tone: "info", values: { names: names(standby) } })
    if (input.sessionAffinity) lines.push({ key: "simAffinity", tone: "info" })
    const unprotected = input.members.filter((member) => !member.breaker)
    if (unprotected.length) {
      lines.push({ key: "simNoBreaker", tone: "warn", values: { names: unprotected.map((member) => label(member.backend_id)) } })
    }
    if (!input.failing.size) lines.push({ key: "simHint", tone: "info" })
    return lines
  }

  const failing = input.members.filter((member) => input.failing.has(member.backend_id))
  const failingNames = failing.map((member) => label(member.backend_id))

  if (phase === "failing") {
    lines.push({ key: input.kind === "throttle" ? "simFailing429" : "simFailing5xx", tone: "error", values: { names: failingNames } })
    const hit = result.members.filter((member) => member.failing && member.share > 0)
    for (const item of hit) {
      const breaker = byId.get(item.backend_id)?.breaker ?? null
      const name = label(item.backend_id)
      if (!breaker) lines.push({ key: "simNoBreakerMember", tone: "error", values: { name } })
      else if (!breakerCatches(breaker, input.kind)) lines.push({ key: "simBreakerMisses", tone: "error", values: { name } })
      else {
        lines.push({
          key: breaker.accept_retry_after && input.kind === "throttle" ? "simTripRuleRetryAfter" : "simTripRule",
          tone: "trip",
          values: { name, count: breaker.failure_count, window: breaker.interval_seconds, trip: breaker.trip_duration_seconds },
        })
      }
    }
    const idle = result.members.filter((member) => member.failing && member.share === 0)
    if (idle.length) lines.push({ key: "simFailingIdle", tone: "info", values: { names: names(idle) } })
    if (hit.length) {
      if (input.kind === "outage") lines.push({ key: "simOutageNoRetry", tone: "warn" })
      else if (input.retry) {
        lines.push({
          key: input.retry.firstFast ? "simRetry429Fast" : "simRetry429",
          tone: "info",
          values: { attempts: input.retry.attempts, interval: input.retry.interval },
        })
      } else if (input.bound) lines.push({ key: "simNoRetry429", tone: "warn" })
    }
    if (result.outcome === "ok" && hit.length) lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  const tripped = simulate(input, "tripped")
  const trippedItems = tripped.members.filter((member) => member.state === "tripped")

  if (phase === "tripped") {
    if (!trippedItems.length) lines.push({ key: "simNothingTripped", tone: "info" })
    else lines.push({ key: "simTripped", tone: "trip", values: { names: names(trippedItems) } })
    if (result.outcome === "down") {
      lines.push({ key: "simAllDown", tone: "error" })
      return lines
    }
    const normal = simulate(input, "normal")
    if (trippedItems.length && result.servingPriority !== null) {
      lines.push(result.servingPriority !== normal.servingPriority
        ? { key: "simFailoverTo", tone: "ok", values: { priority: result.servingPriority + 1, names: names(serving) } }
        : { key: "simShiftTo", tone: "ok", values: { names: serving.map((item) => `${label(item.backend_id)} ${percent(item.share)}`) } })
    }
    const stillFailing = result.members.filter((member) => member.failing && member.share > 0)
    for (const item of stillFailing) {
      lines.push({ key: "simStillFailing", tone: "error", values: { name: label(item.backend_id), share: percent(item.share) } })
    }
    if (result.outcome === "ok") lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  // recovered
  if (!trippedItems.length) {
    lines.push({ key: "simNothingTripped", tone: "info" })
    return lines
  }
  const retryAfter = input.kind === "throttle"
    && trippedItems.some((item) => byId.get(item.backend_id)?.breaker?.accept_retry_after)
  const durations = [...new Set(trippedItems.map((item) => byId.get(item.backend_id)?.breaker?.trip_duration_seconds ?? 0))]
  lines.push({
    key: retryAfter ? "simRecoveredRetryAfter" : "simRecovered",
    tone: "ok",
    values: { names: names(trippedItems), trip: durations.join("/") },
  })
  lines.push({ key: "simRetrip", tone: "info" })
  return lines
}
