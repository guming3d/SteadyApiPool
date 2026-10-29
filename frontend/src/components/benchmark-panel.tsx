// The call guide's load test: simulated users calling the pool through the gateway, run by the
// app's backend (pool_manager/benchmark.py), with live numbers, two charts in the Monitor tab's
// style, the members that answered, a verdict, exports and the same run as a terminal command.
// The key is the one the call guide's key step holds; the backend never returns or keeps it.
import { CircleAlert, CircleCheck, Download, LoaderCircle, OctagonAlert, Play, Square, TriangleAlert } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys } from "./api"
import { memberOfRow } from "./api-test"
import {
  chartPoints, cliCommand, DEFAULT_SPEC, estimate, invalidFields, LIMITS, presetOf, PRESETS, presetSpec, PROMPT_TOKENS,
  runJson, timelineCsv, verdict, type ChartPoint, type Preset,
} from "./benchmark"
import { fill, useI18n, type LitellmMessages } from "../i18n"
import { formatCompact, formatMs, formatPercent, niceScale, roundedTop } from "../pool-metrics"
import { isGuest } from "../session"
import type { BenchmarkRun, BenchmarkSpec, PromptSize } from "./types"
import { GuestNotice } from "./api-tester"
import { CodeBlock } from "./call-guide"
import { Grid, HEIGHT, Legend, PAD, PLOT_HEIGHT, Tile, Tooltip, useWidth } from "../components/pool-monitor"
import { Button, ErrorText } from "../components/ui"

type Messages = LitellmMessages

const PRESET_LABEL: Record<Preset, "benchPresetSmoke" | "benchPresetTypical" | "benchPresetStress"> = {
  smoke: "benchPresetSmoke",
  typical: "benchPresetTypical",
  stress: "benchPresetStress",
}
const PROMPT_LABEL: Record<PromptSize, "benchPromptShort" | "benchPromptMedium" | "benchPromptLong"> = {
  short: "benchPromptShort",
  medium: "benchPromptMedium",
  long: "benchPromptLong",
}
const STATE_LABEL: Record<BenchmarkRun["state"], "benchRunning" | "benchDone" | "benchStopped" | "benchFailed"> = {
  running: "benchRunning",
  done: "benchDone",
  stopped: "benchStopped",
  failed: "benchFailed",
}

function duration(seconds: number, t: Messages) {
  return seconds >= 60 && seconds % 60 === 0 ? fill(t.benchMinutes, { n: seconds / 60 }) : fill(t.benchSeconds, { n: seconds })
}

function download(name: string, type: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = name
  link.click()
  URL.revokeObjectURL(url)
}

type LoadTestProps = {
  poolId: string
  baseUrl: string
  virtualKey: string
  model: string
  canWrite: boolean
}

export function LoadTest(props: LoadTestProps) {
  return isGuest() ? <GuestNotice /> : <LoadTestBody {...props} />
}

function NumberField({ label, hint, value, min, max, optional, invalid, onChange }: {
  label: string
  hint?: string
  value: number | null
  min: number
  max: number
  optional?: boolean
  invalid: boolean
  onChange: (value: number | null) => void
}) {
  return <label className="field">{label}
    <input type="number" inputMode="numeric" min={min} max={max} value={value ?? ""} aria-invalid={invalid}
      placeholder={optional ? "—" : undefined}
      onChange={(event) => onChange(event.target.value === "" ? (optional ? null : Number.NaN) : Number(event.target.value))} />
    {hint && <small>{hint}</small>}
  </label>
}

function LoadTestBody({ poolId, baseUrl, virtualKey, model, canWrite }: LoadTestProps) {
  const { t, locale } = useI18n("litellm")
  const queryClient = useQueryClient()
  const [spec, setSpec] = useState<BenchmarkSpec>(DEFAULT_SPEC)
  const [runId, setRunId] = useState<string | null>(null)
  const set = (patch: Partial<BenchmarkSpec>) => setSpec((current) => ({ ...current, ...patch }))

  const run = useQuery({
    queryKey: keys.benchmark(poolId, runId ?? ""),
    queryFn: () => api.benchmark(poolId, runId!),
    enabled: runId !== null,
    refetchInterval: (query) => query.state.data?.state === "running" ? 1000 : false,
  })
  const start = useMutation({
    mutationFn: () => api.startBenchmark(poolId, { spec: { ...spec, model }, key: virtualKey }),
    onSuccess: (started) => {
      queryClient.setQueryData(keys.benchmark(poolId, started.id), started)
      setRunId(started.id)
    },
  })
  const stop = useMutation({
    mutationFn: () => api.stopBenchmark(poolId, runId!),
    onSuccess: (stopped) => queryClient.setQueryData(keys.benchmark(poolId, stopped.id), stopped),
  })

  const bad = invalidFields(spec)
  const running = run.data?.state === "running"
  const preset = presetOf(spec)
  const guess = estimate(spec)

  return <>
    <p>{t.benchHint}</p>
    <div className="bench-presets" role="group" aria-label={t.benchPresets}>
      {PRESETS.map((item) => {
        const values = presetSpec(item)
        return <button key={item} type="button" className={item === preset ? "active" : ""} aria-pressed={item === preset} disabled={running}
          onClick={() => setSpec(presetSpec(item, spec))}>
          <b>{t[PRESET_LABEL[item]]}</b>
          <span>{fill(t.benchPresetSub, { users: values.users, duration: duration(values.duration_seconds, t) }, locale)}</span>
        </button>
      })}
      {!preset && <span className="tag" data-tone="warn">{t.benchCustom}</span>}
    </div>
    <details className="bench-advanced" open={bad.length > 0 || undefined}>
      <summary>{t.benchAdvanced}</summary>
      <div className="form-grid">
        <NumberField label={t.benchUsers} value={spec.users} min={1} max={LIMITS.users} invalid={bad.includes("users")} onChange={(value) => set({ users: value ?? Number.NaN })} />
        <NumberField label={t.benchDuration} value={spec.duration_seconds} min={1} max={LIMITS.duration_seconds} invalid={bad.includes("duration_seconds")} onChange={(value) => set({ duration_seconds: value ?? Number.NaN })} />
        <NumberField label={t.benchRampUp} hint={t.benchRampUpHint} value={spec.ramp_up_seconds} min={0} max={LIMITS.duration_seconds} invalid={bad.includes("ramp_up_seconds")} onChange={(value) => set({ ramp_up_seconds: value ?? Number.NaN })} />
        <NumberField label={t.benchRate} hint={t.benchRateHint} value={spec.rate} min={0} max={LIMITS.rate} optional invalid={bad.includes("rate")} onChange={(value) => set({ rate: value })} />
        <NumberField label={t.benchMaxRequests} value={spec.max_requests} min={1} max={LIMITS.max_requests} invalid={bad.includes("max_requests")} onChange={(value) => set({ max_requests: value ?? Number.NaN })} />
        <NumberField label={t.benchMaxTokens} value={spec.max_tokens} min={1} max={LIMITS.max_tokens} invalid={bad.includes("max_tokens")} onChange={(value) => set({ max_tokens: value ?? Number.NaN })} />
        <label className="field">{t.benchPrompt}
          <select value={spec.prompt_size} onChange={(event) => set({ prompt_size: event.target.value as PromptSize })}>
            {(Object.keys(PROMPT_TOKENS) as PromptSize[]).map((size) => <option key={size} value={size}>{t[PROMPT_LABEL[size]]}</option>)}
          </select>
        </label>
        <label className="check"><input type="checkbox" checked={spec.stream} onChange={(event) => set({ stream: event.target.checked })} />{t.benchStream}</label>
      </div>
      <small className="muted">{fill(t.benchLimits, { users: LIMITS.users, seconds: LIMITS.duration_seconds, requests: formatCompact(LIMITS.max_requests) }, locale)}</small>
    </details>
    {bad.length === 0
      ? <p>{fill(t.benchEstimate, { model, requests: formatCompact(guess.requests), input: formatCompact(guess.inputTokens), output: formatCompact(guess.outputTokens) }, locale)}</p>
      : <p className="warn-text">{t.benchInvalid}</p>}
    <p className="bench-cost"><CircleAlert size={13} />{t.benchCost}</p>
    {!canWrite && <p className="muted">{t.benchNeedsWrite}</p>}
    {canWrite && !virtualKey && <p className="warn-text">{t.testNeedsKey}</p>}
    <div className="bench-actions">
      {running
        ? <Button variant="outline" disabled={stop.isPending} onClick={() => stop.mutate()}>
          {stop.isPending ? <LoaderCircle className="spin" size={13} /> : <Square size={13} />}{stop.isPending ? t.benchStopping : t.benchStop}
        </Button>
        : <Button variant="primary" disabled={!canWrite || !virtualKey || bad.length > 0 || start.isPending} onClick={() => start.mutate()}>
          {start.isPending ? <LoaderCircle className="spin" size={13} /> : <Play size={13} />}{t.benchStart}
        </Button>}
    </div>
    <ErrorText error={start.error ?? stop.error ?? run.error} />
    {run.data && <Result run={run.data} poolId={poolId} baseUrl={baseUrl} t={t} />}
  </>
}

function Verdict({ run, t }: { run: BenchmarkRun; t: Messages }) {
  const { locale } = useI18n("litellm")
  const { tone, parts } = verdict(run.summary)
  const Icon = tone === "ok" ? CircleCheck : tone === "warn" ? TriangleAlert : OctagonAlert
  return <div className="bench-verdict" data-tone={tone} role="status">
    <Icon size={15} />
    <span>{parts.map((part) => fill(t[part.key], part.values, locale)).join(locale === "zh" ? "" : " ")}</span>
  </div>
}

function Result({ run, poolId, baseUrl, t }: { run: BenchmarkRun; poolId: string; baseUrl: string; t: Messages }) {
  const { locale } = useI18n("litellm")
  const summary = run.summary
  const { tone } = verdict(summary)
  const progress = Math.min(1, Math.max(summary.elapsed_seconds / run.spec.duration_seconds, summary.requests / run.spec.max_requests))
  const errors = Object.entries(summary.errors)
  const other = errors.reduce((sum, [, count]) => sum + count, 0)
  const stamp = run.started_at.replace(/[:+]/g, "").slice(0, 15)
  const windows = typeof navigator !== "undefined" && navigator.userAgent.includes("Windows")
  return <div className="monitor bench-result">
    <div className="bench-status">
      <span className="tag" data-tone={run.state === "done" ? "ok" : run.state === "running" ? undefined : "warn"}>
        {run.state === "running" && <LoaderCircle className="spin" size={11} />}{t[STATE_LABEL[run.state]]}
      </span>
      <div className="bench-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)}>
        <div style={{ width: `${progress * 100}%` }} />
      </div>
      <span className="muted">{fill(t.benchProgress, {
        elapsed: Math.round(summary.elapsed_seconds), duration: run.spec.duration_seconds, requests: formatCompact(summary.requests), model: run.model,
      }, locale)}</span>
    </div>
    {run.error && <div className="callout warning"><CircleAlert size={15} /><div>{fill(t.benchFailedRun, { error: run.error })}</div></div>}
    {run.state !== "running" && <Verdict run={run} t={t} />}
    <div className="kpi-row">
      <Tile t={t} label={t.benchThroughput} value={fill(t.benchPerSecond, { n: summary.requests_per_second })}
        sub={fill(t.benchThroughputSub, { requests: formatCompact(summary.requests), tokens: formatCompact(summary.output_tokens_per_second) }, locale)} />
      <Tile t={t} label={t.benchSuccess} value={formatPercent(summary.success_rate)} tone={summary.requests ? tone : "ok"}
        sub={fill(t.benchSuccessSub, { throttled: summary.throttled, errors: other, failovers: summary.failovers }, locale)} />
      <Tile t={t} label={t.benchLatency} value={formatMs(summary.latency_p50_ms)}
        sub={fill(t.benchLatencySub, { p95: formatMs(summary.latency_p95_ms), p99: formatMs(summary.latency_p99_ms), max: formatMs(summary.latency_max_ms) })} />
      <Tile t={t} label={t.benchTtft} value={formatMs(summary.ttft_p50_ms)}
        sub={run.spec.stream ? fill(t.benchTtftSub, { p95: formatMs(summary.ttft_p95_ms) }) : t.benchNoStream} />
    </div>
    {errors.length > 0 && <p className="monitor-note muted">
      {fill(t.benchErrorList, { list: errors.map(([label, count]) => `${label} × ${count}`).join(", ") })}
      {summary.errors["401"] ? ` ${t.diagKey}` : ""}
    </p>}
    <div className="monitor-charts">
      <ThroughputChart points={chartPoints(summary.timeline)} t={t} />
      <LatencyChart points={chartPoints(summary.timeline)} t={t} />
    </div>
    {summary.members.length > 0 && <section className="monitor-section">
      <header><b>{t.benchMembers}</b><span className="muted">{t.benchMembersHint}</span></header>
      <table className="monitor-members">
        <thead><tr><th>{t.monMember}</th><th>{t.monShare}</th><th className="num">{t.monRequests}</th><th className="num">{t.benchP50}</th><th className="num">{t.monErrors}</th></tr></thead>
        <tbody>{summary.members.map((member) => <tr key={member.member ?? ""}>
          <td><b>{memberOfRow(member.member) ?? t.benchNoMember}</b></td>
          <td><div className="share"><div className="share-track"><div className="share-fill" style={{ width: `${member.share * 100}%` }} /></div>
            <span><b>{formatPercent(member.share, 0)}</b></span></div></td>
          <td className="num">{formatCompact(member.requests)}</td>
          <td className="num">{formatMs(member.latency_p50_ms)}</td>
          <td className="num">{formatCompact(member.errors)}</td>
        </tr>)}</tbody>
      </table>
    </section>}
    {run.state !== "running" && <div className="bench-actions">
      <Button variant="outline" onClick={() => download(`load-test-${poolId}-${stamp}.json`, "application/json", runJson(run))}><Download size={13} />{t.benchExportJson}</Button>
      <Button variant="outline" onClick={() => download(`load-test-${poolId}-${stamp}.csv`, "text/csv", timelineCsv(run))}><Download size={13} />{t.benchExportCsv}</Button>
    </div>}
    <details className="call-notes">
      <summary>{t.benchCli}</summary>
      <p className="muted">{t.benchCliHint}</p>
      <CodeBlock code={cliCommand({ url: baseUrl, model: run.model, spec: run.spec, windows })} label={t.benchCli} />
    </details>
  </div>
}

const THROUGHPUT_SERIES = ["success", "throttled", "errors"] as const

function SecondsAxis({ width, points, t }: { width: number; points: ChartPoint[]; t: Messages }) {
  if (!points.length) return null
  const plot = width - PAD.left - PAD.right
  const picks = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])]
  return <g className="viz-axis">
    {picks.map((index) => {
      const x = PAD.left + (points.length === 1 ? plot / 2 : (index / (points.length - 1)) * plot)
      const anchor = index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"
      return <text key={index} x={x} y={HEIGHT - 6} textAnchor={anchor}>{fill(t.benchSeconds, { n: points[index]!.second })}</text>
    })}
  </g>
}

function rate(value: number) {
  return value >= 10 ? formatCompact(value) : String(Math.round(value * 10) / 10)
}

function ThroughputChart({ points, t }: { points: ChartPoint[]; t: Messages }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const labels = { success: t.monSuccess, throttled: t.monThrottled429, errors: t.monErrors }
  const scale = niceScale(Math.max(0, ...points.map((point) => point.success + point.throttled + point.errors)))
  const plot = Math.max(0, width - PAD.left - PAD.right)
  const slot = points.length ? plot / points.length : 0
  const bar = Math.max(1, Math.min(24, slot - 2))
  const height = (value: number) => (value / scale.max) * PLOT_HEIGHT
  const hovered = hover === null ? null : points[hover]
  return <figure className="viz-card">
    <figcaption><b>{t.benchChartThroughput}</b><Legend items={THROUGHPUT_SERIES.map((key) => ({ key, label: labels[key] }))} /></figcaption>
    <div className="viz-plot" ref={ref} onPointerLeave={() => setHover(null)}>
      {width > 0 && <svg width={width} height={HEIGHT} aria-hidden="true">
        <Grid width={width} ticks={scale.ticks} max={scale.max} format={rate} />
        {points.map((point, index) => {
          const x = PAD.left + index * slot + (slot - bar) / 2
          const present = THROUGHPUT_SERIES.filter((key) => point[key] > 0)
          let base = PAD.top + PLOT_HEIGHT
          return <g key={index} className={hover !== null && hover !== index ? "viz-dim" : undefined}>
            {present.map((key, position) => {
              const h = height(point[key])
              base -= h
              const gap = position > 0 && h > 2 ? 2 : 0
              return position === present.length - 1
                ? <path key={key} d={roundedTop(x, base, bar, h - gap)} data-series={key} />
                : <rect key={key} x={x} y={base} width={bar} height={Math.max(0, h - gap)} data-series={key} />
            })}
            <rect className="viz-hit" x={PAD.left + index * slot} y={PAD.top} width={slot} height={PLOT_HEIGHT} onPointerEnter={() => setHover(index)} />
          </g>
        })}
        <SecondsAxis width={width} points={points} t={t} />
      </svg>}
      {hovered && hover !== null && <Tooltip left={PAD.left + (hover + 0.5) * slot} width={width} title={fill(t.benchAtSecond, { n: hovered.second })}
        rows={THROUGHPUT_SERIES.map((key) => ({ key, label: labels[key], value: fill(t.benchPerSecond, { n: rate(hovered[key]) }) }))} />}
    </div>
    <table className="sr-only">
      <caption>{t.benchChartThroughput}</caption>
      <thead><tr><th>{t.monTime}</th>{THROUGHPUT_SERIES.map((key) => <th key={key}>{labels[key]}</th>)}</tr></thead>
      <tbody>{points.map((point) => <tr key={point.second}><td>{fill(t.benchSeconds, { n: point.second })}</td>{THROUGHPUT_SERIES.map((key) => <td key={key}>{rate(point[key])}</td>)}</tr>)}</tbody>
    </table>
  </figure>
}

function LatencyChart({ points, t }: { points: ChartPoint[]; t: Messages }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const scale = niceScale(Math.max(0, ...points.map((point) => point.latencyMs ?? 0)))
  const plot = Math.max(0, width - PAD.left - PAD.right)
  const x = (index: number) => PAD.left + (points.length > 1 ? (index / (points.length - 1)) * plot : plot / 2)
  const y = (ms: number) => PAD.top + PLOT_HEIGHT - (ms / scale.max) * PLOT_HEIGHT
  // Seconds without a success have no latency: the line breaks instead of dropping to zero.
  const path = points.reduce((d, point, index) => {
    if (point.latencyMs === null) return d
    const previous = index > 0 ? points[index - 1]!.latencyMs : null
    return `${d}${previous === null ? "M" : "L"}${x(index).toFixed(1)},${y(point.latencyMs).toFixed(1)}`
  }, "")
  const track = (clientX: number, element: Element) => {
    const offset = clientX - element.getBoundingClientRect().left
    const index = points.length > 1 ? Math.round(((offset - PAD.left) / plot) * (points.length - 1)) : 0
    setHover(Math.max(0, Math.min(points.length - 1, index)))
  }
  let last: number | null = null
  for (let index = points.length - 1; index >= 0 && last === null; index--) if (points[index]!.latencyMs !== null) last = index
  const dot = hover ?? last
  const dotMs = dot === null ? null : points[dot]!.latencyMs
  const hovered = hover === null ? null : points[hover]
  return <figure className="viz-card">
    <figcaption><b>{t.benchChartLatency}</b><Legend items={[{ key: "overall", label: t.benchP50, line: true }]} /></figcaption>
    <div className="viz-plot" ref={ref} onPointerLeave={() => setHover(null)}>
      {width > 0 && <svg width={width} height={HEIGHT} aria-hidden="true" onPointerMove={(event) => track(event.clientX, event.currentTarget)}>
        <Grid width={width} ticks={scale.ticks} max={scale.max} format={(ms) => ms ? formatMs(ms) : "0"} />
        {hover !== null && <line className="viz-crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + PLOT_HEIGHT} />}
        <path className="viz-line" d={path} data-series="overall" />
        {dot !== null && dotMs !== null && <circle className="viz-dot" cx={x(dot)} cy={y(dotMs)} r={4} data-series="overall" />}
        <SecondsAxis width={width} points={points} t={t} />
      </svg>}
      {hovered && hover !== null && <Tooltip left={x(hover)} width={width} title={fill(t.benchAtSecond, { n: hovered.second })}
        rows={[{ key: "overall", label: t.benchP50, value: formatMs(hovered.latencyMs) }]} />}
    </div>
    <table className="sr-only">
      <caption>{t.benchChartLatency}</caption>
      <thead><tr><th>{t.monTime}</th><th>{t.benchP50}</th></tr></thead>
      <tbody>{points.map((point) => <tr key={point.second}><td>{fill(t.benchSeconds, { n: point.second })}</td><td>{formatMs(point.latencyMs)}</td></tr>)}</tbody>
    </table>
  </figure>
}
