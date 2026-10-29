// What a pool's Foundry deployments allow, from the model check (backend model_check.py): all
// together, and as the pool's priorities and weights split the traffic; and whether that covers
// the traffic a gateway was sized for. Pure, so node --test can import it.
import type { ModelCheck, PoolMember } from "./types"

export type QuotaSummary = {
  tpm: number
  rpm: number
  /** Some members' limits are estimated from capacity, not reported by Azure. */
  estimated: boolean
  /** Members whose limits can't be read (custom endpoints, provisioned throughput). */
  unknown: number
  /** Below what the gateway was sized for, when that is known. */
  short: boolean
}

export type SizedFor = { tpm: number; rpm: number }

/** The traffic a sizing scenario means at its peak, in tokens and requests a minute. */
export function sizedFor(sizing: { peak_rpm: number; input_tokens: number; output_tokens: number } | null | undefined): SizedFor | null {
  if (!sizing) return null
  return { tpm: sizing.peak_rpm * (sizing.input_tokens + sizing.output_tokens), rpm: sizing.peak_rpm }
}

/** null while nothing about the pool's quota is known. */
export function quotaSummary(check: ModelCheck | undefined, sized: SizedFor | null): QuotaSummary | null {
  if (!check || (check.total_tpm === null && check.total_rpm === null)) return null
  const tpm = check.total_tpm ?? 0
  const rpm = check.total_rpm ?? 0
  return {
    tpm,
    rpm,
    estimated: check.members.some((member) => member.quota_source === "capacity"),
    unknown: check.members.filter((member) => member.tpm === null && member.rpm === null).length,
    short: sized !== null && (tpm < sized.tpm || rpm < sized.rpm),
  }
}

export type SplitLimit = {
  /** Null when an active member's limit can't be read. */
  tpm: number | null
  rpm: number | null
  /** Members taking traffic while all are healthy: 1 in failover. */
  active: number
}

/**
 * What the pool takes, split as its members are set, before its first deployment is
 * rate-limited (and the breaker or cooldown moves that deployment's share to the others). As
 * the backend's expected_shares: the highest priority (lowest number) splits the traffic by
 * weight, the others stand by. Failover: the primary's own limit. Balanced and weighted: the
 * lowest of each active member's limit over its share.
 */
export function splitLimit(check: ModelCheck | undefined, members: PoolMember[]): SplitLimit | null {
  if (!check || !members.length) return null
  const top = Math.min(...members.map((member) => member.priority))
  const active = members.filter((member) => member.priority === top)
  const weights = active.reduce((sum, member) => sum + Math.max(member.weight, 0), 0)
  const byId = new Map(check.members.map((member) => [member.backend_id.toLowerCase(), member]))
  const limit = (field: "tpm" | "rpm") => {
    let lowest = Infinity
    for (const member of active) {
      const share = weights ? Math.max(member.weight, 0) / weights : 1 / active.length
      if (share <= 0) continue
      const value = byId.get(member.backend_id.toLowerCase())?.[field]
      if (value === null || value === undefined) return null
      lowest = Math.min(lowest, value / share)
    }
    return Number.isFinite(lowest) ? Math.round(lowest) : null
  }
  return { tpm: limit("tpm"), rpm: limit("rpm"), active: active.length }
}

/**
 * Weights in proportion to each member's tokens a minute (requests a minute when no member
 * reports tokens), scaled so the largest is 100 and none below 1, the range weights take. Null
 * when a member's limit is unknown, or when they would all be equal (that is balanced, not
 * weighted).
 */
export function capacityWeights(check: ModelCheck | undefined, members: PoolMember[]): PoolMember[] | null {
  if (!check || members.length < 2) return null
  const byId = new Map(check.members.map((member) => [member.backend_id.toLowerCase(), member]))
  const limits = members.map((member) => byId.get(member.backend_id.toLowerCase()))
  const field = limits.every((limit) => limit?.tpm) ? "tpm" : limits.every((limit) => limit?.rpm) ? "rpm" : null
  if (!field) return null
  const values = limits.map((limit) => limit![field]!)
  const largest = Math.max(...values)
  const weights = values.map((value) => Math.min(100, Math.max(1, Math.round(value / largest * 100))))
  if (weights.every((weight) => weight === weights[0])) return null
  return members.map((member, index) => ({ ...member, weight: weights[index]! }))
}

/** Tokens a minute as quota is quoted: 1.5M, 200K. */
export function formatRate(value: number, locale: string): string {
  const number = (n: number, digits: number) => new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(n)
  if (value >= 1_000_000) return `${number(value / 1_000_000, 1)}M`
  if (value >= 10_000) return `${number(value / 1000, 0)}K`
  return number(value, 0)
}
