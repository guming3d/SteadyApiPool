// The pool and gateway dashboards' arithmetic: formatting, chart scales and health thresholds.
// Pure, so node --test can import it.
import type { GatewayMetrics, LoadSeries, MemberMetrics, MetricsRange } from "./types"

export const RANGES: MetricsRange[] = ["1h", "6h", "24h", "7d"]

export type Tone = "ok" | "warn" | "bad"

/** 1,284 -> "1,284"; 12,900 -> "12.9K"; 4,200,000 -> "4.2M". */
export function formatCompact(value: number) {
  const abs = Math.abs(value)
  if (abs < 10_000) return Math.round(value).toLocaleString("en-US")
  for (const [limit, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]] as const) {
    if (abs >= limit) {
      const scaled = value / limit
      return `${scaled >= 100 ? Math.round(scaled) : scaled.toFixed(1).replace(/\.0$/, "")}${suffix}`
    }
  }
  return String(value)
}

export function formatMs(ms: number | null | undefined) {
  if (ms === null || ms === undefined) return "—"
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`
}

/** Shares and rates: 0.99952 -> "99.95%", 0.004 -> "0.4%". Never rounds to a false 100%. */
export function formatPercent(value: number | null | undefined, digits = 1) {
  if (value === null || value === undefined) return "—"
  const percent = value * 100
  let text = percent.toFixed(digits)
  if (percent < 100 && Number(text) >= 100) text = (100 - 10 ** -digits).toFixed(digits)
  return `${text.replace(/\.0+$/, "")}%`
}

/** A clean axis maximum and its ticks (0, half, max). */
export function niceScale(max: number): { max: number; ticks: number[] } {
  if (!(max > 0)) return { max: 1, ticks: [0, 1] }
  const magnitude = 10 ** Math.floor(Math.log10(max))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((candidate) => candidate >= max) ?? 10 * magnitude
  return { max: step, ticks: [0, step / 2, step] }
}

/** 99.9% and up is healthy; below 99% clients are noticing. */
export function availabilityTone(availability: number | null): Tone {
  if (availability === null) return "ok"
  return availability >= 0.999 ? "ok" : availability >= 0.99 ? "warn" : "bad"
}

/** Share of requests answered 429: each one means every member was busy, even after retries. */
export function throttleTone(throttled: number, requests: number): Tone {
  if (!requests) return "ok"
  const rate = throttled / requests
  return rate < 0.001 ? "ok" : rate < 0.05 ? "warn" : "bad"
}

export function errorCount(gateway: Pick<GatewayMetrics, "client_errors" | "server_errors">) {
  return gateway.client_errors + gateway.server_errors
}

/** Members' share of the pool's deployment traffic; null when none of them has numbers. */
export function actualShares(members: MemberMetrics[]): (number | null)[] {
  const total = members.reduce((sum, member) => sum + (member.requests ?? 0), 0)
  return members.map((member) => member.requests === null || !total ? null : member.requests / total)
}

/** Actual traffic far from what priority and weight intend: a member is failing or saturated. */
export function shareTone(actual: number | null, expected: number): Tone {
  if (actual === null) return "ok"
  const gap = Math.abs(actual - expected)
  return gap < 0.15 ? "ok" : gap < 0.3 ? "warn" : "bad"
}

/** Axis and tooltip time: HH:MM for short ranges, MM-DD HH:MM for a week. */
export function formatTime(iso: string, range: MetricsRange) {
  const date = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, "0")
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return range === "7d" ? `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}` : time
}

/** Requests per minute over the whole window. */
export function perMinute(requests: number, start: string, end: string) {
  const minutes = (Date.parse(end) - Date.parse(start)) / 60_000
  return minutes > 0 ? requests / minutes : 0
}

/** A rectangle with only its top corners rounded (column data-ends; square at the baseline). */
export function roundedTop(x: number, y: number, width: number, height: number, radius = 4) {
  const r = Math.max(0, Math.min(radius, width / 2, height))
  return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`
}

/** A load value: whole percentages; counts compact, with a decimal for small averages (1.5
 *  replicas). */
export function formatLoad(value: number | null | undefined, unit: LoadSeries["unit"]) {
  if (value === null || value === undefined) return "—"
  if (unit === "percent") return `${Math.round(value)}%`
  return value < 10 && !Number.isInteger(value) ? value.toFixed(1) : formatCompact(value)
}

/** How loaded a resource is now: a share above 70% is busy, above 90% saturated; replicas at
 *  80% of their maximum leave little room, at the maximum none (unless it is one replica, as
 *  on a dev gateway, which never scales). */
export function loadTone(series: Pick<LoadSeries, "unit" | "latest" | "limit">): Tone {
  const latest = series.latest
  if (latest === null) return "ok"
  if (series.unit === "percent") return latest >= 90 ? "bad" : latest >= 70 ? "warn" : "ok"
  if (!series.limit || series.limit <= 1) return "ok"
  return latest >= series.limit ? "bad" : latest >= 0.8 * series.limit ? "warn" : "ok"
}
