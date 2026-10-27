// What a LiteLLM gateway does with a pool, as plain data for the routing preview.
// Behaviour follows LiteLLM's router (checked against a real proxy, see backend/tests/
// litellm_stack): requests go to the lowest priority ("order") whose members are not cooling
// down, split by weight inside it. A failed attempt (429 or 5xx alike) counts against the member;
// more than allowed_fails in a minute and it sits out cooldown_seconds. Within one request LiteLLM
// retries on another member, and when a priority runs out it falls through to the next one. It
// never health-checks: a member only cools down when real requests fail on it, and only gets
// traffic again once its cooldown ends.
import type { PoolMember } from "../types"
import type { CircuitBreaker } from "./types"

export type FailureKind = "throttle" | "outage"
export type Phase = "normal" | "failing" | "tripped" | "recovered"
export const PHASES: readonly Phase[] = ["normal", "failing", "tripped", "recovered"]

export type SimInput = {
  members: PoolMember[]
  breaker: CircuitBreaker
  /** Further attempts for a failed request. */
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
/** ok: clients see no errors. degraded: some requests fail. down: every member is out. */
export type Outcome = "ok" | "degraded" | "down"
export type SimResult = { members: MemberResult[]; outcome: Outcome; servingPriority: number | null }

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

function cooledMembers(input: SimInput) {
  // A member cools down only once failing requests reach it, so follow the traffic: cooling a
  // primary can send traffic to a standby that is failing too.
  const cooled = new Set<string>()
  for (let round = 0; round <= input.members.length; round++) {
    const { shares } = route(input.members, cooled)
    const next = input.members.filter((member) =>
      input.failing.has(member.backend_id) && (shares.get(member.backend_id) ?? 0) > 0)
    if (!next.length) break
    for (const member of next) cooled.add(member.backend_id)
  }
  return cooled
}

/** Within one request: where a failed attempt goes next, if anywhere. */
export function rescue(input: SimInput, failedId: string): "retry" | "fallback" | null {
  const healthy = input.members.filter((member) =>
    member.backend_id !== failedId && !input.failing.has(member.backend_id))
  if (!healthy.length) return null
  const priority = input.members.find((member) => member.backend_id === failedId)?.priority ?? 0
  // Retries pick again inside the same priority; a priority that runs out falls through.
  if (input.retries > 0 && healthy.some((member) => member.priority === priority)) return "retry"
  if (healthy.some((member) => member.priority > priority)) return "fallback"
  return input.retries > 0 ? "retry" : null
}

export function simulate(input: SimInput, phase: Phase): SimResult {
  const failingNow = phase === "failing" || phase === "tripped"
  const cooled = phase === "tripped" ? cooledMembers(input) : new Set<string>()
  const { shares, priority } = route(input.members, cooled)
  const members = input.members.map((member): MemberResult => {
    const share = shares.get(member.backend_id) ?? 0
    return {
      backend_id: member.backend_id,
      priority: member.priority,
      state: cooled.has(member.backend_id) ? "tripped" : share > 0 ? "serving" : "standby",
      share,
      failing: failingNow && input.failing.has(member.backend_id) && !cooled.has(member.backend_id),
    }
  })
  return { members, outcome: outcome(input, members, priority), servingPriority: priority }
}

function outcome(input: SimInput, members: MemberResult[], priority: number | null): Outcome {
  if (priority === null) return "down"
  const hit = members.filter((member) => member.failing && member.share > 0)
  if (!hit.length) return "ok"
  // The failed attempt is answered by another member within the same request.
  return hit.every((member) => rescue(input, member.backend_id) !== null) ? "ok" : "degraded"
}

export type Tone = "info" | "ok" | "warn" | "error" | "trip"
export type Line = { key: NarrationKey; tone: Tone; values?: Record<string, string | number | string[]> }

/** Every message key the narration can produce; each must exist in i18n.tsx. */
export type NarrationKey =
  | "simServingOne" | "simServingSplit" | "simStandby" | "simHint" | "simFailing429"
  | "simFailing5xx" | "simCooldownRule" | "simFailingIdle" | "simRetryOther" | "simFallbackNext"
  | "simNoRescue" | "simTripped" | "simAllDown" | "simFailoverTo" | "simShiftTo" | "simClientOk"
  | "simNothingTripped" | "simRecovered" | "simRetrip"

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

  if (phase === "failing") {
    lines.push({ key: input.kind === "throttle" ? "simFailing429" : "simFailing5xx", tone: "error", values: { names: failingNames } })
    const hit = result.members.filter((member) => member.failing && member.share > 0)
    for (const item of hit) {
      lines.push({
        key: "simCooldownRule",
        tone: "trip",
        values: {
          name: label(item.backend_id),
          count: input.breaker.allowed_fails + 1,
          cooldown: input.breaker.cooldown_seconds,
        },
      })
      const next = rescue(input, item.backend_id)
      lines.push(next === "retry"
        ? { key: "simRetryOther", tone: "info", values: { name: label(item.backend_id), retries: input.retries } }
        : next === "fallback"
          ? { key: "simFallbackNext", tone: "info", values: { name: label(item.backend_id) } }
          : { key: "simNoRescue", tone: "warn", values: { name: label(item.backend_id) } })
    }
    const idle = result.members.filter((member) => member.failing && member.share === 0)
    if (idle.length) lines.push({ key: "simFailingIdle", tone: "info", values: { names: names(idle) } })
    if (result.outcome === "ok" && hit.length) lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  const cooled = simulate(input, "tripped")
  const cooledItems = cooled.members.filter((member) => member.state === "tripped")

  if (phase === "tripped") {
    if (!cooledItems.length) lines.push({ key: "simNothingTripped", tone: "info" })
    else lines.push({ key: "simTripped", tone: "trip", values: { names: names(cooledItems), cooldown: input.breaker.cooldown_seconds } })
    if (result.outcome === "down") {
      lines.push({ key: "simAllDown", tone: "error" })
      return lines
    }
    const normal = simulate(input, "normal")
    if (cooledItems.length && result.servingPriority !== null) {
      lines.push(result.servingPriority !== normal.servingPriority
        ? { key: "simFailoverTo", tone: "ok", values: { priority: result.servingPriority + 1, names: names(serving) } }
        : { key: "simShiftTo", tone: "ok", values: { names: serving.map((item) => `${label(item.backend_id)} ${percent(item.share)}`) } })
    }
    if (result.outcome === "ok") lines.push({ key: "simClientOk", tone: "ok" })
    return lines
  }

  // recovered
  if (!cooledItems.length) {
    lines.push({ key: "simNothingTripped", tone: "info" })
    return lines
  }
  lines.push({
    key: "simRecovered",
    tone: "ok",
    values: { names: names(cooledItems), cooldown: input.breaker.cooldown_seconds },
  })
  lines.push({ key: "simRetrip", tone: "info" })
  return lines
}
