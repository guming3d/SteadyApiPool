// What an AI Gateway alias does with a pool, as plain data for the routing preview. Behaviour
// follows the alias's routing (Microsoft's AI Gateway docs and labs): requests go to the lowest
// priority whose backend models are available, split by weight inside it. When a backend fails
// and the alias's failure rule matches the failure, the gateway moves the request on (another
// model in the same priority first, then the next priority), as long as the retry limit
// (maxRetryAttempts) has sends left, and keeps the failed backend out of the alias for
// excludeFor. A failure the rule doesn't match goes to the client as it is, and excludes
// nothing; with no sends left, a matched failure answers an empty 503. Pure, so node --test can
// import it.
import type { PoolMember } from "../types"
import type { CircuitBreaker } from "./types"

export type FailureKind = "throttle" | "outage"
export type Phase = "normal" | "failing" | "tripped" | "recovered"
export const PHASES: readonly Phase[] = ["normal", "failing", "tripped", "recovered"]

export type SimInput = {
  members: PoolMember[]
  breaker: CircuitBreaker
  /** Extra sends of one request across the members. */
  retries: number
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
export type Outcome = "ok" | "degraded" | "down"
export type SimResult = { members: MemberResult[]; outcome: Outcome; servingPriority: number | null }

/** The status a failure of this kind answers with, as the rule sees it. */
export const failureStatus = (kind: FailureKind) => kind === "throttle" ? 429 : 503

/** Whether the alias's failure rule catches this kind of failure. */
export function ruleMatches(breaker: CircuitBreaker, kind: FailureKind) {
  const status = failureStatus(kind)
  return breaker.status_code_ranges.some((item) => status >= item.minimum && status <= item.maximum)
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

function excludedMembers(input: SimInput) {
  // A member is excluded once a failing request reaches it and the rule matches; follow the
  // traffic, since excluding a primary can send traffic to a standby that is failing too.
  const excluded = new Set<string>()
  if (!ruleMatches(input.breaker, input.kind) || input.breaker.exclude_seconds <= 0) return excluded
  for (let round = 0; round <= input.members.length; round++) {
    const { shares } = route(input.members, excluded)
    const next = input.members.filter((member) =>
      input.failing.has(member.backend_id) && (shares.get(member.backend_id) ?? 0) > 0)
    if (!next.length) break
    for (const member of next) excluded.add(member.backend_id)
  }
  return excluded
}

/** Within one request: where a failed attempt goes next, if anywhere. */
export function rescue(input: SimInput, failedId: string): "retry" | "fallback" | null {
  if (!ruleMatches(input.breaker, input.kind) || input.retries <= 0) return null
  const healthy = input.members.filter((member) =>
    member.backend_id !== failedId && !input.failing.has(member.backend_id))
  if (!healthy.length) return null
  const priority = input.members.find((member) => member.backend_id === failedId)?.priority ?? 0
  if (healthy.some((member) => member.priority === priority)) return "retry"
  return healthy.some((member) => member.priority > priority) ? "fallback" : "retry"
}

export function simulate(input: SimInput, phase: Phase): SimResult {
  const failingNow = phase === "failing" || phase === "tripped"
  const excluded = phase === "tripped" ? excludedMembers(input) : new Set<string>()
  const { shares, priority } = route(input.members, excluded)
  const members = input.members.map((member): MemberResult => {
    const share = shares.get(member.backend_id) ?? 0
    return {
      backend_id: member.backend_id,
      priority: member.priority,
      state: excluded.has(member.backend_id) ? "tripped" : share > 0 ? "serving" : "standby",
      share,
      failing: failingNow && input.failing.has(member.backend_id) && !excluded.has(member.backend_id),
    }
  })
  return { members, outcome: outcome(input, members, priority), servingPriority: priority }
}

function outcome(input: SimInput, members: MemberResult[], priority: number | null): Outcome {
  if (priority === null) return "down"
  const hit = members.filter((member) => member.failing && member.share > 0)
  if (!hit.length) return "ok"
  return hit.every((member) => rescue(input, member.backend_id) !== null) ? "ok" : "degraded"
}

export type Tone = "info" | "ok" | "warn" | "error" | "trip"
export type Line = { key: NarrationKey; tone: Tone; values?: Record<string, string | number | string[]> }

/** Every message key the narration can produce; each must exist in messages.ts. */
export type NarrationKey =
  | "simServingOne" | "simServingSplit" | "simStandby" | "simHint" | "simFailing429"
  | "simFailing5xx" | "simExcludeRule" | "simNoRule" | "simNoRetries" | "simFailingIdle"
  | "simRetryOther" | "simFallbackNext" | "simNoRescue" | "simTripped" | "simAllDown"
  | "simFailoverTo" | "simShiftTo" | "simClientOk" | "simNothingTripped" | "simRecovered" | "simRetrip"

const percent = (value: number) => `${Math.round(value)}%`

export function narrate(input: SimInput, phase: Phase, label: (backendId: string) => string): Line[] {
  const result = simulate(input, phase)
  const names = (items: MemberResult[]) => items.map((item) => label(item.backend_id))
  const serving = result.members.filter((member) => member.state === "serving")
  const lines: Line[] = []

  if (phase === "normal") {
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
    if (!input.failing.size) lines.push({ key: "simHint", tone: "info" })
    return lines
  }

  const failing = input.members.filter((member) => input.failing.has(member.backend_id))
  const failingNames = failing.map((member) => label(member.backend_id))
  const matches = ruleMatches(input.breaker, input.kind)
  const code = String(failureStatus(input.kind))

  if (phase === "failing") {
    lines.push({ key: input.kind === "throttle" ? "simFailing429" : "simFailing5xx", tone: "error", values: { names: failingNames } })
    const hit = result.members.filter((member) => member.failing && member.share > 0)
    for (const item of hit) {
      if (!matches) {
        lines.push({ key: "simNoRule", tone: "warn", values: { name: label(item.backend_id), code } })
        continue
      }
      lines.push({
        key: "simExcludeRule",
        tone: "trip",
        values: { name: label(item.backend_id), cooldown: input.breaker.exclude_seconds },
      })
      const next = rescue(input, item.backend_id)
      lines.push(next === "retry"
        ? { key: "simRetryOther", tone: "info", values: { name: label(item.backend_id), retries: input.retries } }
        : next === "fallback"
          ? { key: "simFallbackNext", tone: "info", values: { name: label(item.backend_id) } }
          : input.retries <= 0
            ? { key: "simNoRetries", tone: "warn", values: { name: label(item.backend_id) } }
            : { key: "simNoRescue", tone: "warn", values: { name: label(item.backend_id) } })
    }
    const idle = result.members.filter((member) => member.failing && member.share === 0)
    if (idle.length) lines.push({ key: "simFailingIdle", tone: "info", values: { names: names(idle) } })
    if (result.outcome === "ok" && hit.length) lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  const excluded = simulate(input, "tripped")
  const excludedItems = excluded.members.filter((member) => member.state === "tripped")

  if (phase === "tripped") {
    if (!excludedItems.length) lines.push({ key: "simNothingTripped", tone: "info" })
    else lines.push({ key: "simTripped", tone: "trip", values: { names: names(excludedItems), cooldown: input.breaker.exclude_seconds } })
    if (result.outcome === "down") {
      lines.push({ key: "simAllDown", tone: "error" })
      return lines
    }
    const normal = simulate(input, "normal")
    if (excludedItems.length && result.servingPriority !== null) {
      lines.push(result.servingPriority !== normal.servingPriority
        ? { key: "simFailoverTo", tone: "ok", values: { priority: result.servingPriority + 1, names: names(serving) } }
        : { key: "simShiftTo", tone: "ok", values: { names: serving.map((item) => `${label(item.backend_id)} ${percent(item.share)}`) } })
    }
    if (result.outcome === "ok") lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  // recovered
  if (!excludedItems.length) {
    lines.push({ key: "simNothingTripped", tone: "info" })
    return lines
  }
  lines.push({ key: "simRecovered", tone: "ok", values: { names: names(excludedItems), cooldown: input.breaker.exclude_seconds } })
  lines.push({ key: "simRetrip", tone: "info" })
  return lines
}
