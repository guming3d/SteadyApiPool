// The load test's arithmetic: presets, the estimate shown before a run, the verdict after it,
// the chart's buckets, the exports and the matching terminal command. Pure, so node --test can
// import it. The presets and limits mirror pool_manager/benchmark.py and models.py.
import type { BenchmarkPoint, BenchmarkRun, BenchmarkSpec, BenchmarkSummary, PromptSize } from "./types"

export type Preset = "smoke" | "typical" | "stress"
export const PRESETS: Preset[] = ["smoke", "typical", "stress"]

export const LIMITS = { users: 50, duration_seconds: 600, max_requests: 5000, rate: 1000, max_tokens: 4096 } as const

export const DEFAULT_SPEC: BenchmarkSpec = {
  users: 2,
  duration_seconds: 30,
  max_requests: LIMITS.max_requests,
  ramp_up_seconds: 0,
  rate: null,
  prompt_size: "short",
  max_tokens: 128,
  stream: true,
  model: null,
}

const PRESET_VALUES: Record<Preset, Pick<BenchmarkSpec, "users" | "duration_seconds" | "ramp_up_seconds">> = {
  smoke: { users: 2, duration_seconds: 30, ramp_up_seconds: 0 },
  typical: { users: 10, duration_seconds: 120, ramp_up_seconds: 10 },
  stress: { users: 30, duration_seconds: 180, ramp_up_seconds: 30 },
}

/** About how many input tokens each prompt size sends. */
export const PROMPT_TOKENS: Record<PromptSize, number> = { short: 50, medium: 500, long: 4000 }

export function presetSpec(preset: Preset, base: BenchmarkSpec = DEFAULT_SPEC): BenchmarkSpec {
  return { ...base, ...PRESET_VALUES[preset] }
}

/** The preset a spec's load matches, or null once it was changed by hand. */
export function presetOf(spec: BenchmarkSpec): Preset | null {
  return PRESETS.find((preset) => {
    const values = PRESET_VALUES[preset]
    return spec.users === values.users && spec.duration_seconds === values.duration_seconds
      && spec.ramp_up_seconds === values.ramp_up_seconds
  }) ?? null
}

/** What is wrong with a spec, as the keys of the fields out of range (empty when it can run). */
export function invalidFields(spec: BenchmarkSpec): Array<keyof BenchmarkSpec> {
  const bad: Array<keyof BenchmarkSpec> = []
  const whole = (value: number, min: number, max: number) => Number.isInteger(value) && value >= min && value <= max
  if (!whole(spec.users, 1, LIMITS.users)) bad.push("users")
  if (!whole(spec.duration_seconds, 1, LIMITS.duration_seconds)) bad.push("duration_seconds")
  if (!whole(spec.max_requests, 1, LIMITS.max_requests)) bad.push("max_requests")
  if (!whole(spec.ramp_up_seconds, 0, LIMITS.duration_seconds)) bad.push("ramp_up_seconds")
  if (spec.rate !== null && !(spec.rate > 0 && spec.rate <= LIMITS.rate)) bad.push("rate")
  if (!whole(spec.max_tokens, 1, LIMITS.max_tokens)) bad.push("max_tokens")
  return bad
}

/** A rough answer time for one request, before anything is measured. */
export function assumedLatencyMs(spec: Pick<BenchmarkSpec, "max_tokens" | "prompt_size">) {
  const prompt = spec.prompt_size === "long" ? 800 : spec.prompt_size === "medium" ? 200 : 0
  return 400 + spec.max_tokens * 12 + prompt
}

/** About how many requests and tokens a run sends: users keep one request in flight each. */
export function estimate(spec: BenchmarkSpec) {
  const latency = assumedLatencyMs(spec) / 1000
  let userSeconds = 0
  for (let user = 0; user < spec.users; user++) {
    const joins = spec.users > 1 ? (spec.ramp_up_seconds * user) / spec.users : 0
    userSeconds += Math.max(0, spec.duration_seconds - joins)
  }
  let requests = userSeconds / latency
  if (spec.rate !== null) requests = Math.min(requests, spec.rate * spec.duration_seconds)
  requests = Math.max(1, Math.min(spec.max_requests, Math.round(requests)))
  return {
    requests,
    inputTokens: requests * PROMPT_TOKENS[spec.prompt_size],
    // At most: answers may be shorter than max_tokens.
    outputTokens: requests * spec.max_tokens,
  }
}

export type Tone = "ok" | "warn" | "bad"

export type VerdictPart =
  | { key: "benchVerdictNone"; values: Record<string, never> }
  | { key: "benchVerdictRate"; values: { rate: string; p95: string } }
  | { key: "benchVerdictOk"; values: Record<string, never> }
  | { key: "benchVerdictThrottled"; values: { count: number; percent: string } }
  | { key: "benchVerdictErrors"; values: { percent: string } }
  | { key: "benchVerdictFailovers"; values: { count: number } }

function percent(value: number) {
  return `${Math.round(value * 1000) / 10}%`.replace(/\.0%$/, "%")
}

function seconds(ms: number | null) {
  return ms === null ? "—" : ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`
}

/** The run in a few plain sentences: how much it handled, what went wrong, what LiteLLM did. */
export function verdict(summary: BenchmarkSummary): { tone: Tone; parts: VerdictPart[] } {
  if (!summary.requests) return { tone: "warn", parts: [{ key: "benchVerdictNone", values: {} }] }
  const rate = summary.success_rate ?? 0
  const parts: VerdictPart[] = [{
    key: "benchVerdictRate",
    values: { rate: String(Math.round(summary.requests_per_second * 10) / 10), p95: seconds(summary.latency_p95_ms) },
  }]
  if (summary.throttled) {
    parts.push({ key: "benchVerdictThrottled", values: { count: summary.throttled, percent: percent(summary.throttled / summary.requests) } })
  } else if (rate < 0.99) {
    parts.push({ key: "benchVerdictErrors", values: { percent: percent(rate) } })
  } else {
    parts.push({ key: "benchVerdictOk", values: {} })
  }
  if (summary.failovers) parts.push({ key: "benchVerdictFailovers", values: { count: summary.failovers } })
  return { tone: rate >= 0.99 ? "ok" : rate >= 0.9 ? "warn" : "bad", parts }
}

export type ChartPoint = {
  /** First second of the bucket. */
  second: number
  seconds: number
  /** Per second, over the bucket. */
  success: number
  throttled: number
  errors: number
  latencyMs: number | null
}

/** The timeline in at most ``max`` buckets, so a 10-minute run still draws readable bars. */
export function chartPoints(timeline: BenchmarkPoint[], max = 90): ChartPoint[] {
  const size = Math.max(1, Math.ceil(timeline.length / max))
  const points: ChartPoint[] = []
  for (let start = 0; start < timeline.length; start += size) {
    const bucket = timeline.slice(start, start + size)
    const requests = bucket.reduce((sum, point) => sum + point.requests, 0)
    const throttled = bucket.reduce((sum, point) => sum + point.throttled, 0)
    const errors = bucket.reduce((sum, point) => sum + point.errors, 0)
    // Each second's median latency, weighted by its successful requests.
    let weight = 0
    let total = 0
    for (const point of bucket) {
      const ok = point.requests - point.errors - point.throttled
      if (point.latency_p50_ms !== null && ok > 0) {
        weight += ok
        total += point.latency_p50_ms * ok
      }
    }
    points.push({
      second: bucket[0]!.second,
      seconds: bucket.length,
      success: (requests - throttled - errors) / bucket.length,
      throttled: throttled / bucket.length,
      errors: errors / bucket.length,
      latencyMs: weight ? total / weight : null,
    })
  }
  return points
}

/** The per-second timeline as CSV. */
export function timelineCsv(run: BenchmarkRun) {
  const rows = run.summary.timeline.map((point) =>
    [point.second, point.users, point.requests, point.errors, point.throttled, point.latency_p50_ms ?? ""].join(","))
  return ["second,users,requests,errors,throttled,latency_p50_ms", ...rows].join("\n") + "\n"
}

/** The run as JSON (it holds no key). */
export function runJson(run: BenchmarkRun) {
  return JSON.stringify(run, null, 2) + "\n"
}

/** The same run from a terminal, with scripts/benchmark.py; the key goes in an environment
 * variable so it stays out of the shell history. */
export function cliCommand({ url, model, spec, windows }: { url: string; model: string; spec: BenchmarkSpec; windows: boolean }) {
  const args = ["--url", url, "--model", model, "--users", String(spec.users), "--duration", String(spec.duration_seconds)]
  if (spec.ramp_up_seconds) args.push("--ramp-up", String(spec.ramp_up_seconds))
  if (spec.rate !== null) args.push("--rate", String(spec.rate))
  if (spec.max_requests !== DEFAULT_SPEC.max_requests) args.push("--max-requests", String(spec.max_requests))
  if (spec.prompt_size !== DEFAULT_SPEC.prompt_size) args.push("--prompt", spec.prompt_size)
  if (spec.max_tokens !== DEFAULT_SPEC.max_tokens) args.push("--max-tokens", String(spec.max_tokens))
  if (!spec.stream) args.push("--no-stream")
  const python = windows ? "backend\\.venv\\Scripts\\python scripts\\benchmark.py" : "backend/.venv/bin/python scripts/benchmark.py"
  const key = windows ? '$env:LITELLM_KEY = "<your-virtual-key>"' : "export LITELLM_KEY='<your-virtual-key>'"
  return `${key}\n${python} ${args.join(" ")}`
}
