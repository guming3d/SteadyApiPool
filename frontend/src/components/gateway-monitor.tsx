// The Gateway metrics tab: everything that goes through the gateway (APIM's whole service, or LiteLLM's
// spans of every model) and the load on what it runs on (APIM's capacity, or its CPU and memory
// on the v2 tiers; LiteLLM's replicas and their CPU and memory, or the AKS nodes, and its
// database). The tiles and charts are the pool Monitor's (pool-monitor.tsx).
import { CircleAlert, ExternalLink, LoaderCircle, RefreshCw } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import { formatLoad, loadTone, RANGES } from "../pool-metrics"
import { useProductUi } from "../product"
import type { LoadProblem, LoadSeries, MetricsRange } from "../types"
import { LatencyChart, LineChart, Tiles, ToneIcon, TrafficChart } from "./pool-monitor"
import { Button, ErrorText, FieldHelp } from "./ui"

type Messages = ReturnType<typeof useI18n>["t"]

function LoadChart({ series, range, t }: { series: LoadSeries; range: MetricsRange; t: Messages }) {
  const tone = loadTone(series)
  const format = (value: number | null) => formatLoad(value, series.unit)
  const peaks = series.points.some((point) => point.peak !== null)
  return <LineChart
    title={<span className="load-title">
      <span><b>{t[`load_${series.metric}`]}</b><FieldHelp>{t[`loadHint_${series.metric}`]}</FieldHelp><span className="muted">{series.resource}</span></span>
      <span className="load-now" data-tone={tone}>
        {tone !== "ok" && <ToneIcon tone={tone} size={12} />}
        {t.gwNow} <b>{format(series.latest)}</b>
        <span className="muted"> · {fill(t.gwPeakSub, { peak: format(series.peak), average: format(series.average) })}
          {series.limit != null && ` · ${fill(t.gwLimit, { limit: format(series.limit) })}`}</span>
      </span>
    </span>}
    times={series.points.map((point) => point.time)}
    series={[
      { key: "average", label: t.gwAverage, values: series.points.map((point) => point.average) },
      ...peaks ? [{ key: "peak", label: t.gwPeak, values: series.points.map((point) => point.peak) }] : [],
    ]}
    range={range}
    format={format}
    axisMax={series.unit === "percent" ? 100 : series.limit ?? 0}
  />
}

function ProblemNote({ problem, t }: { problem: LoadProblem; t: Messages }) {
  const message = problem.error === "forbidden" ? t.gwForbidden
    : problem.error === "forbidden_resources" ? t.gwGroupForbidden
    : null
  return <div className="callout warning"><CircleAlert size={15} /><div>
    <span>{message ? fill(message, { resource: problem.resource }) : problem.error}</span>
  </div></div>
}

export function GatewayMonitor() {
  const { product, useFacts } = useProductUi()
  const { t } = useI18n(product)
  const facts = useFacts()
  const queryClient = useQueryClient()
  const [range, setRange] = useState<MetricsRange>("1h")
  const load = useQuery(queries.gatewayLoad(product, range))
  const refresh = useMutation({
    mutationFn: () => api.gatewayLoad(product, range, true),
    onSuccess: (data) => queryClient.setQueryData(keys.gatewayLoad(product, range), data),
  })
  const data = load.data
  const updated = load.dataUpdatedAt ? new Date(load.dataUpdatedAt).toLocaleTimeString() : null
  const busy = load.isFetching || refresh.isPending
  const traffic = data?.traffic

  return <div className="page">
    <header className="page-header"><div><h1>{t.gwTitle}</h1>{facts && <code>{facts.name}</code>}</div></header>
    <p className="muted page-hint">{t.gwHint}</p>
    <div className="monitor" data-fetching={busy && Boolean(data)}>
      <div className="monitor-bar">
        <div className="segment" role="group" aria-label={t.monRange}>
          {RANGES.map((candidate) => <button key={candidate} type="button" className={candidate === range ? "active" : ""} aria-pressed={candidate === range} onClick={() => setRange(candidate)}>{t[`monRange${candidate}`]}</button>)}
        </div>
        <span className="muted monitor-updated">{updated && fill(t.monUpdated, { time: updated })}</span>
        <Button variant="ghost" disabled={busy} onClick={() => refresh.mutate()}><RefreshCw size={13} className={busy ? "spin" : ""} />{t.monRefresh}</Button>
        {facts?.metricsUrl && <a className="monitor-portal" href={facts.metricsUrl} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.gwOpenPortal}</a>}
      </div>
      {!data
        ? load.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.monLoading}</div> : <ErrorText error={load.error} />
        : <>
          <ErrorText error={refresh.error ?? (load.isError ? load.error : null)} />
          <section className="monitor-section">
            <header><b>{t.gwTraffic}</b><span className="muted">{t.gwTrafficHint}</span></header>
            {data.traffic_error === "no_app_insights"
              ? <div className="state">{t.gwNoAppInsights}</div>
              : data.traffic_error === "forbidden"
                ? <div className="callout warning"><CircleAlert size={15} /><div><span>{t.monForbidden}</span><small className="muted">{t.monPermissionHint}</small></div></div>
                : data.traffic_error && <div className="callout warning"><CircleAlert size={15} /><div><span>{data.traffic_error}</span><small className="muted">{t.monPermissionHint}</small></div></div>}
            {traffic && <>
              <Tiles gateway={traffic} start={data.start} end={data.end} t={t} />
              {traffic.requests
                ? <div className="monitor-charts">
                  <TrafficChart points={traffic.traffic} range={data.range} t={t} />
                  <LatencyChart points={traffic.latency} range={data.range} t={t} />
                </div>
                : <div className="state">{t.monNoTraffic}</div>}
            </>}
          </section>
          <section className="monitor-section">
            <header><b>{t.gwResources}</b></header>
            {data.problems.map((problem) => <ProblemNote key={`${problem.resource}:${problem.error}`} problem={problem} t={t} />)}
            {data.series.length
              ? <div className="monitor-charts">{data.series.map((series) => <LoadChart key={`${series.resource}:${series.metric}`} series={series} range={data.range} t={t} />)}</div>
              : !data.problems.length && <div className="state">{t.gwNoResources}</div>}
          </section>
          <p className="muted monitor-note">{t.gwDelayNote}</p>
        </>}
    </div>
  </div>
}
