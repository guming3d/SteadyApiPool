// What a pool's Foundry deployments allow, from the model check (backend model_check.py), and
// whether that covers the traffic a gateway was sized for. Pure, so node --test can import it.
import type { ModelCheck } from "./types"

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

/** Tokens a minute as quota is quoted: 1.5M, 200K. */
export function formatRate(value: number, locale: string): string {
  const number = (n: number, digits: number) => new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(n)
  if (value >= 1_000_000) return `${number(value / 1_000_000, 1)}M`
  if (value >= 10_000) return `${number(value / 1000, 0)}K`
  return number(value, 0)
}
