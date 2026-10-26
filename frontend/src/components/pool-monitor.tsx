// The pool's Monitor tab: what its clients saw (APIM metrics for the pool's APIs) and how each
// member deployment is doing (its own Azure OpenAI metrics). Deliberately small: four tiles, two
// charts and one table.
import { CircleAlert, ExternalLink, LoaderCircle, OctagonAlert, RefreshCw, TriangleAlert } from "lucide-react"
import { useLayoutEffect, useRef, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import {
  actualShares, availabilityTone, formatCompact, formatMs, formatPercent, formatTime,
  niceScale, perMinute, RANGES, roundedTop, shareTone, throttleTone, type Tone,
} from "../pool-metrics"
import type { LatencyPoint, MemberMetrics, MetricsRange, PoolMetrics, TrafficPoint } from "../types"
import { Button, ErrorText } from "./ui"

type Messages = ReturnType<typeof useI18n>["t"]

const HEIGHT = 168
const PAD = { left: 44, right: 10, top: 10, bottom: 22 }
const PLOT_HEIGHT = HEIGHT - PAD.top - PAD.bottom

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return
    // Hidden tab panels measure 0 until shown; the observer catches the first real size.
    const observer = new ResizeObserver(([entry]) => setWidth(entry!.contentRect.width))
    observer.observe(element)
    setWidth(element.clientWidth)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

function ToneIcon({ tone, size = 13 }: { tone: Tone; size?: number }) {
  if (tone === "bad") return <OctagonAlert size={size} />
  if (tone === "warn") return <TriangleAlert size={size} />
  return null
}

function Tile({ label, value, sub, tone = "ok", t }: { label: string; value: string; sub: ReactNode; tone?: Tone; t: Messages }) {
  return <div className="kpi" data-tone={tone}>
    <span className="kpi-label">{label}</span>
    <span className="kpi-value">{value}</span>
    {tone !== "ok" && <span className="kpi-status"><ToneIcon tone={tone} />{tone === "bad" ? t.monCritical : t.monDegraded}</span>}
    <span className="kpi-sub">{sub}</span>
  </div>
}

/** Y gridlines and tick labels, shared by both charts. */
function Grid({ width, ticks, max, format }: { width: number; ticks: number[]; max: number; format: (value: number) => string }) {
  return <g className="viz-grid">
    {ticks.map((tick) => {
      const y = PAD.top + PLOT_HEIGHT - (tick / max) * PLOT_HEIGHT
      return <g key={tick}>
        <line x1={PAD.left} x2={width - PAD.right} y1={y} y2={y} />
        <text x={PAD.left - 6} y={y} dy="0.32em" textAnchor="end">{format(tick)}</text>
      </g>
    })}
  </g>
}

function TimeAxis({ width, times, range }: { width: number; times: string[]; range: MetricsRange }) {
  if (!times.length) return null
  const plot = width - PAD.left - PAD.right
  const picks = [...new Set([0, Math.floor((times.length - 1) / 2), times.length - 1])]
  return <g className="viz-axis">
    {picks.map((index) => {
      const x = PAD.left + (times.length === 1 ? plot / 2 : (index / (times.length - 1)) * plot)
      const anchor = index === 0 ? "start" : index === times.length - 1 ? "end" : "middle"
      return <text key={index} x={x} y={HEIGHT - 6} textAnchor={anchor}>{formatTime(times[index]!, range)}</text>
    })}
  </g>
}

function Tooltip({ left, width, title, rows }: { left: number; width: number; title: string; rows: Array<{ key: string; label: string; value: string }> }) {
  // Stays inside the chart: flips to the left of the pointer in the right half.
  const style = left > width / 2 ? { right: width - left + 10 } : { left: left + 10 }
  return <div className="viz-tooltip" style={style} role="status">
    <span className="viz-tooltip-title">{title}</span>
    {rows.map((row) => <span key={row.key} className="viz-tooltip-row">
      {/* Line keys at tooltip density; the legend mirrors the marks. */}
      <i className="key-line" data-series={row.key} />
      <b>{row.value}</b><span>{row.label}</span>
    </span>)}
  </div>
}

function Legend({ items }: { items: Array<{ key: string; label: string; line?: boolean }> }) {
  return <div className="viz-legend">
    {items.map((item) => <span key={item.key}><i className={item.line ? "key-line" : "key-box"} data-series={item.key} />{item.label}</span>)}
  </div>
}

const TRAFFIC_SERIES = ["success", "throttled", "errors"] as const

function TrafficChart({ points, range, t }: { points: TrafficPoint[]; range: MetricsRange; t: Messages }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const labels = { success: t.monSuccess, throttled: t.monThrottled429, errors: t.monErrors }
  const stacks = points.map((point) => ({ success: point.success, throttled: point.throttled, errors: point.client_errors + point.server_errors }))
  const scale = niceScale(Math.max(0, ...stacks.map((s) => s.success + s.throttled + s.errors)))
  const plot = Math.max(0, width - PAD.left - PAD.right)
  const slot = points.length ? plot / points.length : 0
  // At most 24px wide; a 2px surface gap between neighbours.
  const bar = Math.max(1, Math.min(24, slot - 2))
  const height = (value: number) => (value / scale.max) * PLOT_HEIGHT
  const hovered = hover === null ? null : points[hover]
  return <figure className="viz-card">
    <figcaption><b>{t.monTraffic}</b><Legend items={TRAFFIC_SERIES.map((key) => ({ key, label: labels[key] }))} /></figcaption>
    <div className="viz-plot" ref={ref} onPointerLeave={() => setHover(null)}>
      {width > 0 && <svg width={width} height={HEIGHT} aria-hidden="true">
        <Grid width={width} ticks={scale.ticks} max={scale.max} format={formatCompact} />
        {stacks.map((stack, index) => {
          const x = PAD.left + index * slot + (slot - bar) / 2
          const present = TRAFFIC_SERIES.filter((key) => stack[key] > 0)
          let base = PAD.top + PLOT_HEIGHT
          return <g key={index} className={hover !== null && hover !== index ? "viz-dim" : undefined}>
            {present.map((key, position) => {
              const h = height(stack[key])
              base -= h
              // Every segment above the first leaves a 2px surface gap below itself.
              const gap = position > 0 && h > 2 ? 2 : 0
              return position === present.length - 1
                ? <path key={key} d={roundedTop(x, base, bar, h - gap)} data-series={key} />
                : <rect key={key} x={x} y={base} width={bar} height={Math.max(0, h - gap)} data-series={key} />
            })}
            <rect className="viz-hit" x={PAD.left + index * slot} y={PAD.top} width={slot} height={PLOT_HEIGHT} onPointerEnter={() => setHover(index)} />
          </g>
        })}
        <TimeAxis width={width} times={points.map((point) => point.time)} range={range} />
      </svg>}
      {hovered && hover !== null && <Tooltip left={PAD.left + (hover + 0.5) * slot} width={width} title={formatTime(hovered.time, range)}
        rows={TRAFFIC_SERIES.map((key) => ({ key, label: labels[key], value: formatCompact(stacks[hover]![key]) }))} />}
    </div>
    <table className="sr-only">
      <caption>{t.monTraffic}</caption>
      <thead><tr><th>{t.monTime}</th>{TRAFFIC_SERIES.map((key) => <th key={key}>{labels[key]}</th>)}</tr></thead>
      <tbody>{stacks.map((stack, index) => <tr key={index}><td>{formatTime(points[index]!.time, range)}</td>{TRAFFIC_SERIES.map((key) => <td key={key}>{stack[key]}</td>)}</tr>)}</tbody>
    </table>
  </figure>
}

const LATENCY_SERIES = ["overall", "backend"] as const

function LatencyChart({ points, range, t }: { points: LatencyPoint[]; range: MetricsRange; t: Messages }) {
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hover, setHover] = useState<number | null>(null)
  const labels = { overall: t.monLatencyOverall, backend: t.monLatencyBackend }
  const value = (point: LatencyPoint, key: (typeof LATENCY_SERIES)[number]) => key === "overall" ? point.overall_ms : point.backend_ms
  const values = points.flatMap((point) => LATENCY_SERIES.map((key) => value(point, key) ?? 0))
  const scale = niceScale(Math.max(0, ...values))
  const plot = Math.max(0, width - PAD.left - PAD.right)
  const x = (index: number) => PAD.left + (points.length > 1 ? (index / (points.length - 1)) * plot : plot / 2)
  const y = (ms: number) => PAD.top + PLOT_HEIGHT - (ms / scale.max) * PLOT_HEIGHT
  // Minutes without traffic have no latency: the line breaks instead of dropping to zero.
  const path = (key: (typeof LATENCY_SERIES)[number]) => points.reduce((d, point, index) => {
    const ms = value(point, key)
    if (ms === null) return d
    const previous = index > 0 ? value(points[index - 1]!, key) : null
    return `${d}${previous === null ? "M" : "L"}${x(index).toFixed(1)},${y(ms).toFixed(1)}`
  }, "")
  const last = (key: (typeof LATENCY_SERIES)[number]) => {
    for (let index = points.length - 1; index >= 0; index--) if (value(points[index]!, key) !== null) return index
    return null
  }
  const track = (clientX: number, element: Element) => {
    const offset = clientX - element.getBoundingClientRect().left
    const index = points.length > 1 ? Math.round(((offset - PAD.left) / plot) * (points.length - 1)) : 0
    setHover(Math.max(0, Math.min(points.length - 1, index)))
  }
  const hovered = hover === null ? null : points[hover]
  return <figure className="viz-card">
    <figcaption><b>{t.monLatency}</b><Legend items={LATENCY_SERIES.map((key) => ({ key, label: labels[key], line: true }))} /></figcaption>
    <div className="viz-plot" ref={ref} onPointerLeave={() => setHover(null)}>
      {width > 0 && <svg width={width} height={HEIGHT} aria-hidden="true" onPointerMove={(event) => track(event.clientX, event.currentTarget)}>
        <Grid width={width} ticks={scale.ticks} max={scale.max} format={(ms) => ms ? formatMs(ms) : "0"} />
        {hover !== null && <line className="viz-crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + PLOT_HEIGHT} />}
        {LATENCY_SERIES.map((key) => <path key={key} className="viz-line" d={path(key)} data-series={key} />)}
        {LATENCY_SERIES.map((key) => {
          const index = hover ?? last(key)
          const ms = index === null ? null : value(points[index]!, key)
          return index !== null && ms !== null && <circle key={key} className="viz-dot" cx={x(index)} cy={y(ms)} r={4} data-series={key} />
        })}
        <TimeAxis width={width} times={points.map((point) => point.time)} range={range} />
      </svg>}
      {hovered && hover !== null && <Tooltip left={x(hover)} width={width} title={formatTime(hovered.time, range)}
        rows={LATENCY_SERIES.map((key) => ({ key, label: labels[key], value: formatMs(value(hovered, key)) }))} />}
    </div>
    <table className="sr-only">
      <caption>{t.monLatency}</caption>
      <thead><tr><th>{t.monTime}</th>{LATENCY_SERIES.map((key) => <th key={key}>{labels[key]}</th>)}</tr></thead>
      <tbody>{points.map((point) => <tr key={point.time}><td>{formatTime(point.time, range)}</td>{LATENCY_SERIES.map((key) => <td key={key}>{formatMs(value(point, key))}</td>)}</tr>)}</tbody>
    </table>
  </figure>
}

function ShareMeter({ actual, expected, t }: { actual: number | null; expected: number; t: Messages }) {
  const tone = shareTone(actual, expected)
  return <div className="share" data-tone={tone} title={fill(t.monShareTitle, { actual: formatPercent(actual, 0), expected: formatPercent(expected, 0) })}>
    <div className="share-track">
      {actual !== null && <div className="share-fill" style={{ width: `${actual * 100}%` }} />}
      <div className="share-expected" style={{ left: `${expected * 100}%` }} />
    </div>
    <span>{tone !== "ok" && <ToneIcon tone={tone} size={12} />}<b>{formatPercent(actual, 0)}</b> · {fill(t.monShareExpected, { expected: formatPercent(expected, 0) })}</span>
  </div>
}

function MemberTable({ members, t }: { members: MemberMetrics[]; t: Messages }) {
  const shares = actualShares(members)
  return <table className="monitor-members">
    <thead><tr>
      <th>{t.monMember}</th><th>{t.monShare}</th><th className="num">{t.monRequests}</th><th className="num">429</th>
      <th className="num">5xx</th><th className="num">{t.monTokens}</th><th className="num">{t.monLatencyTtlt}</th><th className="num">PTU</th>
    </tr></thead>
    <tbody>{members.map((member, index) => {
      const deployment = member.deployment_id?.split("/deployments/")[1]
      const name = <td><b>{member.backend_id}</b>{deployment && <span className="muted">{deployment}{member.region && ` · ${member.region}`}</span>}</td>
      if (!member.deployment_id) return <tr key={member.backend_id}>{name}<td colSpan={7} className="muted">{t.monNoDeployment}</td></tr>
      if (member.error) return <tr key={member.backend_id}>{name}<td colSpan={7} className="muted"><CircleAlert size={12} /> {member.error}</td></tr>
      const rate = member.requests ? (member.throttled ?? 0) / member.requests : 0
      const throttle = throttleTone(member.throttled ?? 0, member.requests ?? 0)
      return <tr key={member.backend_id}>
        {name}
        <td><ShareMeter actual={shares[index] ?? null} expected={member.expected_share} t={t} /></td>
        <td className="num">{member.requests === null ? "—" : formatCompact(member.requests)}</td>
        <td className="num" data-tone={throttle}>{throttle !== "ok" && <ToneIcon tone={throttle} size={12} />}{member.throttled === null ? "—" : formatCompact(member.throttled)}{rate > 0 && <span className="muted"> {formatPercent(rate)}</span>}</td>
        <td className="num">{member.server_errors === null ? "—" : formatCompact(member.server_errors)}</td>
        <td className="num">{member.tokens === null ? "—" : formatCompact(member.tokens)}</td>
        <td className="num">{formatMs(member.latency_ms)}</td>
        <td className="num">{member.utilization_pct === null ? "—" : `${Math.round(member.utilization_pct)}%`}</td>
      </tr>
    })}</tbody>
  </table>
}

function Tiles({ data, t }: { data: PoolMetrics; t: Messages }) {
  const gateway = data.gateway!
  const throttle = throttleTone(gateway.throttled, gateway.requests)
  const availability = availabilityTone(gateway.availability)
  return <div className="kpi-row">
    <Tile t={t} label={t.monRequests} value={formatCompact(gateway.requests)} sub={fill(t.monPerMinute, { rate: formatCompact(perMinute(gateway.requests, data.start, data.end)) })} />
    <Tile t={t} label={t.monAvailability} value={formatPercent(gateway.availability, 2)} tone={availability}
      sub={fill(t.monErrorsSub, { server: formatCompact(gateway.server_errors), client: formatCompact(gateway.client_errors) })} />
    <Tile t={t} label={t.monThrottled} value={formatCompact(gateway.throttled)} tone={throttle}
      sub={gateway.requests ? fill(t.monThrottledSub, { rate: formatPercent(gateway.throttled / gateway.requests) }) : t.monNoTraffic} />
    <Tile t={t} label={t.monLatencyAvg} value={formatMs(gateway.duration_ms)}
      sub={fill(t.monBackendSub, { ms: formatMs(gateway.backend_duration_ms) })} />
  </div>
}

export function PoolMonitor({ poolId, active, apimResourceId }: { poolId: string; active: boolean; apimResourceId?: string }) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [range, setRange] = useState<MetricsRange>("1h")
  const metrics = useQuery(queries.poolMetrics(poolId, range, active))
  const refresh = useMutation({
    mutationFn: () => api.poolMetrics(poolId, range, true),
    onSuccess: (data) => queryClient.setQueryData(keys.poolMetrics(poolId, range), data),
  })
  const data = metrics.data
  const updated = metrics.dataUpdatedAt ? new Date(metrics.dataUpdatedAt).toLocaleTimeString() : null
  const busy = metrics.isFetching || refresh.isPending
  const gateway = data?.gateway

  return <div className="monitor" data-fetching={busy && Boolean(data)}>
    <div className="monitor-bar">
      <div className="segment" role="group" aria-label={t.monRange}>
        {RANGES.map((candidate) => <button key={candidate} type="button" className={candidate === range ? "active" : ""} aria-pressed={candidate === range} onClick={() => setRange(candidate)}>{t[`monRange${candidate}`]}</button>)}
      </div>
      <span className="muted monitor-updated">{updated && fill(t.monUpdated, { time: updated })}</span>
      <Button variant="ghost" disabled={busy} onClick={() => refresh.mutate()}><RefreshCw size={13} className={busy ? "spin" : ""} />{t.monRefresh}</Button>
      {apimResourceId && <a className="monitor-portal" href={`https://portal.azure.com/#resource${apimResourceId}/metrics`} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.monOpenPortal}</a>}
    </div>
    {!data
      ? metrics.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.monLoading}</div> : <ErrorText error={metrics.error} />
      : <>
        <ErrorText error={refresh.error ?? (metrics.isError ? metrics.error : null)} />
        {data.gateway_error && <div className="callout warning"><CircleAlert size={15} /><div><span>{data.gateway_error}</span><small className="muted">{t.monPermissionHint}</small></div></div>}
        {!data.api_ids.length && <div className="state">{t.monNoApi}</div>}
        {gateway && <>
          <Tiles data={data} t={t} />
          {gateway.requests
            ? <div className="monitor-charts">
              <TrafficChart points={gateway.traffic} range={data.range} t={t} />
              <LatencyChart points={gateway.latency} range={data.range} t={t} />
            </div>
            : <div className="state">{t.monNoTraffic}</div>}
        </>}
        {data.members.length > 0 && <section className="monitor-section">
          <header><b>{t.monMembers}</b><span className="muted">{t.monMembersHint}</span></header>
          <MemberTable members={data.members} t={t} />
        </section>}
        <p className="muted monitor-note">{t.monDelayNote}</p>
      </>}
  </div>
}
