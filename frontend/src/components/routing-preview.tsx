// Live diagram of what the LiteLLM gateway does with the pool draft, with a failure simulator.
// Layout: client → gateway (pool hub) → members → deployments.
import { Cloud, Pause, Play, RotateCcw } from "lucide-react"
import { useEffect, useId, useMemo, useState } from "react"

import { fill, useI18n, type MessageKey } from "../i18n"
import { draftSettings } from "../pool-breaker"
import { memberRole } from "../pool-changes"
import { deploymentMode } from "../pool-modes"
import {
  narrate,
  PHASES,
  simulate,
  type FailureKind,
  type MemberResult,
  type Outcome,
  type Phase,
  type SimInput,
} from "../routing-sim"
import type { BackendView, PoolWrite } from "../types"
import { Button } from "./ui"

// Slow enough to follow one request end to end; each phase lets a dot finish its trip.
const PHASE_MS = 5600
const DOT_SECONDS = 4.2
const PHASE_LABEL: Record<Phase, MessageKey> = {
  normal: "phaseNormal",
  failing: "phaseFailing",
  tripped: "phaseTripped",
  recovered: "phaseRecovered",
}
const OUTCOME_LABEL: Record<Outcome, MessageKey> = {
  ok: "outcomeOk",
  degraded: "outcomeDegraded",
  down: "outcomeDown",
}

// Geometry of the 960-wide viewBox.
const X = { clientL: 16, clientR: 136, gateL: 168, gateR: 616, hub: 300, pillL: 404, pillR: 596, targetL: 660, targetR: 944 }
const TOP = 88
// How far the hub-to-member curves run level before bending.
const BEND = 64
// Radius of the ring around the pool hub; connectors start and end on it.
const HUB_RING = 21

const clip = (text: string, max: number) => text.length > max ? `${text.slice(0, max - 1)}…` : text

type Tone = "ok" | "idle" | "trip" | "error"

function tone(result: MemberResult): Tone {
  if (result.state === "tripped") return "trip"
  if (result.failing) return "error"
  return result.state === "serving" ? "ok" : "idle"
}

export function RoutingPreview({
  draft,
  backendById,
  modelName,
  gatewayName,
  dirty,
  pendingIds,
}: {
  draft: PoolWrite
  /** Keyed by lower-case backend ID. */
  backendById: Map<string, BackendView>
  /** What clients send as the model: the pool ID. */
  modelName: string
  gatewayName?: string
  dirty: boolean
  /** Lower-case IDs of draft members that are not in the published pool yet. */
  pendingIds: ReadonlySet<string>
}) {
  const { t, locale } = useI18n()
  const markerPrefix = `rp${useId().replace(/[^A-Za-z0-9]/g, "")}`
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [kind, setKind] = useState<FailureKind>("throttle")
  const [phaseIndex, setPhaseIndex] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [still] = useState(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true)

  const memberKey = draft.members.map((member) => member.backend_id).join("\n")
  // Members removed from the draft drop out of the simulation too.
  const failing = useMemo(() => {
    const ids = new Set(memberKey.split("\n"))
    return new Set([...selected].filter((id) => ids.has(id)))
  }, [memberKey, selected])
  const phase: Phase = failing.size ? PHASES[phaseIndex] : "normal"
  const animate = failing.size > 0 && playing && !still

  useEffect(() => {
    if (!animate) return
    const timer = window.setInterval(() => setPhaseIndex((index) => (index + 1) % PHASES.length), PHASE_MS)
    return () => window.clearInterval(timer)
  }, [animate])

  const backendOf = (backendId: string) => backendById.get(backendId.toLowerCase())
  const label = (backendId: string) => backendOf(backendId)?.title || backendId
  const settings = draftSettings(draft)
  const input: SimInput = {
    members: draft.members,
    breaker: settings.circuit_breaker,
    retries: settings.retries,
    failing,
    kind,
  }
  const result = simulate(input, phase)
  const lines = narrate(input, phase, label)

  const toggle = (backendId: string) => {
    const next = new Set(failing)
    if (next.has(backendId)) next.delete(backendId)
    else next.add(backendId)
    setSelected(next)
    setPhaseIndex(1)
    setPlaying(true)
  }
  const reset = () => {
    setSelected(new Set())
    setPhaseIndex(0)
  }

  const count = Math.max(draft.members.length, 1)
  const rowH = count <= 4 ? 64 : count <= 8 ? 50 : 40
  const compact = rowH < 50
  const targetH = Math.min(rowH - 12, 52)
  const hubY = TOP + count * rowH / 2
  const gateBottom = TOP + count * rowH + 40
  const height = gateBottom + 16
  const rowY = (index: number) => TOP + rowH * (index + 0.5)
  // Clients reach a pool by its name as soon as it is published; there is nothing to bind.
  const bound = true
  const mode = draft.members.length ? deploymentMode(draft.members) : null
  const hubTone: Tone = result.outcome === "ok" ? "ok" : result.outcome === "down" ? "error" : "trip"

  // Hub to member as a level-in, level-out curve; the request dots ride the same curve.
  const bend = (index: number, endX: number) =>
    `C ${X.hub + BEND} ${hubY} ${X.pillL - BEND} ${rowY(index)} ${endX} ${rowY(index)}`
  const curve = (index: number) => `M ${X.hub + HUB_RING} ${hubY} ${bend(index, X.pillL - 2)}`
  const flowPath = (index: number) =>
    `M ${X.clientR} ${hubY} L ${X.hub} ${hubY} ${bend(index, X.pillL)} L ${X.targetL} ${rowY(index)}`
  const errorPath = (index: number) =>
    `M ${X.targetL} ${rowY(index)} L ${X.pillL} ${rowY(index)} C ${X.pillL - BEND} ${rowY(index)} ${X.hub + BEND} ${hubY} ${X.hub} ${hubY}`

  const status = (item: MemberResult): string => {
    if (!backendOf(item.backend_id)) return t.statusMissing
    if (item.state === "tripped") return fill(t.statusTripped, { cooldown: settings.circuit_breaker.cooldown_seconds })
    if (item.failing) {
      return item.share > 0 ? fill(t.statusFailing, { code: kind === "throttle" ? "429" : "5xx" }) : t.statusFailingIdle
    }
    return item.state === "serving" ? fill(t.statusServing, { share: `${Math.round(item.share)}%` }) : t.statusStandby
  }

  return <section className="routing-preview" aria-label={t.previewTitle}>
    <header className="rp-header">
      <span><b>{t.previewTitle}</b>{dirty && <em className="rp-draft">{t.previewDraft}</em>}</span>
      <span className="rp-legend" aria-hidden="true">
        <i data-tone="request" />{t.legendRequest}
        <i data-tone="error" />{t.legendError}
        <i data-tone="trip" />{t.legendBreaker}
      </span>
    </header>

    {draft.members.length > 0 && <div className="rp-controls">
      <span className="rp-sim-label"><small>{t.simulateHint}</small></span>
      <div className="segment rp-kind" role="group" aria-label={`${t.failThrottle} / ${t.failOutage}`}>
        {(["throttle", "outage"] as const).map((candidate) =>
          <button key={candidate} type="button" className={kind === candidate ? "active" : ""} aria-pressed={kind === candidate} onClick={() => setKind(candidate)}>
            {candidate === "throttle" ? t.failThrottle : t.failOutage}
          </button>)}
      </div>
      {failing.size > 0 && !still && <button type="button" className="icon-button" title={playing ? t.pause : t.play} aria-label={playing ? t.pause : t.play} onClick={() => setPlaying(!playing)}>
        {playing ? <Pause size={14} /> : <Play size={14} />}
      </button>}
      {failing.size > 0 && <Button variant="ghost" onClick={reset}><RotateCcw size={13} />{t.resetSim}</Button>}
    </div>}

    <div className="rp-canvas">
      <svg viewBox={`0 0 960 ${height}`} role="img" aria-label={lines.map((line) => fill(t[line.key], line.values, locale)).join(" ")}>
        <defs>
          {(["ok", "idle", "trip", "error"] as const).map((name) =>
            // Sized in diagram units, so arrowheads don't grow with the line width.
            <marker key={name} id={`${markerPrefix}-${name}`} viewBox="0 0 10 10" refX="8" refY="5" markerUnits="userSpaceOnUse" markerWidth="9" markerHeight="9" orient="auto-start-reverse">
              <path d="M 1 1.5 L 9 5 L 1 8.5 z" className={`rp-arrow rp-tone-${name}`} />
            </marker>)}
          <radialGradient id={`${markerPrefix}-glow`}>
            <stop offset="0%" className="rp-glow-stop" stopOpacity={0.16} />
            <stop offset="100%" className="rp-glow-stop" stopOpacity={0} />
          </radialGradient>
        </defs>

        <g className="rp-client" data-bound={bound}>
          <rect x={X.clientL} y={hubY - 28} width={X.clientR - X.clientL} height={56} rx={10} />
          <text x={(X.clientL + X.clientR) / 2} y={hubY - 4} textAnchor="middle" className="rp-title">{t.client}</text>
          <text x={(X.clientL + X.clientR) / 2} y={hubY + 14} textAnchor="middle" className="rp-sub">
            {clip(modelName, 16)}
          </text>
        </g>

        <rect x={X.gateL} y={14} width={X.gateR - X.gateL} height={gateBottom - 14} rx={16} className="rp-gateway" />
        <Cloud x={X.gateL + 16} y={26} size={22} strokeWidth={1.75} className="rp-cloud" />
        <text x={X.gateL + 46} y={42} className="rp-title">{t.gateway}</text>
        {gatewayName && <text x={X.gateL + 46} y={60} className="rp-sub">{clip(gatewayName, 40)}</text>}
        <text x={(X.gateL + X.gateR) / 2} y={gateBottom - 14} textAnchor="middle" className="rp-sub">
          {fill(settings.retries ? t.retrySummary : t.retryOff, {
            retries: settings.retries,
            count: settings.circuit_breaker.allowed_fails + 1,
            cooldown: settings.circuit_breaker.cooldown_seconds,
          })}
        </text>

        <circle cx={X.hub} cy={hubY} r={Math.min(count * rowH / 2 + 24, 104)} fill={`url(#${markerPrefix}-glow)`} />
        <line x1={X.clientR} y1={hubY} x2={X.hub - HUB_RING - 3} y2={hubY} className={`rp-line rp-tone-${bound ? "ok" : "idle"}`} data-dashed={!bound} markerEnd={`url(#${markerPrefix}-${bound ? "ok" : "idle"})`} />
        {result.members.map((item, index) => {
          const itemTone = tone(item)
          const y = rowY(index)
          return <g key={item.backend_id}>
            <path d={curve(index)} className={`rp-line rp-tone-${itemTone}`} data-dashed={itemTone === "trip" || itemTone === "idle"} markerEnd={`url(#${markerPrefix}-${itemTone})`} />
            <line x1={X.pillR} y1={y} x2={X.targetL - 2} y2={y} className={`rp-line rp-thin rp-tone-${itemTone}`} data-dashed={itemTone !== "ok"} markerEnd={`url(#${markerPrefix}-${itemTone})`} />
          </g>
        })}

        {/* Drawn under the pills and cards so request dots never cover their text. */}
        {bound && !still && <g className="rp-flows" aria-hidden="true">
          {result.members.map((item, index) => {
            if (item.share <= 0) return null
            const dots = item.share > 50 ? 2 : 1
            return <g key={`${phase}-${item.backend_id}-${index}-${count}`}>
              {Array.from({ length: dots }, (_, dot) =>
                <FlowDot key={`r${dot}`} path={flowPath(index)} begin={-(dot / dots) * DOT_SECONDS - index * 0.35} tone="request" />)}
              {item.failing && Array.from({ length: dots }, (_, dot) =>
                <FlowDot key={`e${dot}`} path={errorPath(index)} begin={-(dot / dots) * DOT_SECONDS - DOT_SECONDS / 2} tone="error" seconds={DOT_SECONDS * 0.7} />)}
            </g>
          })}
        </g>}

        {result.members.map((item, index) => {
          const itemTone = tone(item)
          const y = rowY(index)
          const pending = pendingIds.has(item.backend_id.toLowerCase())
          const backend = backendOf(item.backend_id)
          const role = memberRole(draft.members, item.backend_id)
          return <g key={item.backend_id}>
            <g className="rp-pill" data-tone={itemTone} data-pending={pending}>
              <rect x={X.pillL} y={y - 14} width={X.pillR - X.pillL} height={28} rx={14} />
              <text x={X.pillL + 14} y={y + 4}>{clip(fill(t.pillLabel, { role: t[role], priority: item.priority + 1 }), 22)}</text>
              <circle cx={X.pillR - 15} cy={y} r={4} className={`rp-dot-status rp-tone-${itemTone}`} />
            </g>
            <g
              className="rp-target"
              data-tone={itemTone}
              data-pending={pending}
              data-failing={failing.has(item.backend_id)}
              role="button"
              tabIndex={0}
              aria-pressed={failing.has(item.backend_id)}
              aria-label={fill(t.toggleFailure, { name: label(item.backend_id) })}
              onClick={() => toggle(item.backend_id)}
              onKeyDown={(event) => {
                if (event.key !== "Enter" && event.key !== " ") return
                event.preventDefault()
                toggle(item.backend_id)
              }}
            >
              <title>{`${item.backend_id}${backend?.url ? `\n${backend.url}` : ""}\n${status(item)}`}</title>
              <rect x={X.targetL} y={y - targetH / 2} width={X.targetR - X.targetL} height={targetH} rx={10} />
              <polygon
                points={hexagon(X.targetL + 24, y, compact ? 8 : 11)}
                className="rp-hex"
              />
              <circle cx={X.targetL + 24} cy={y} r={compact ? 2.5 : 3} className="rp-hex-core" />
              {compact ? <>
                <text x={X.targetL + 44} y={y + 4.5} className="rp-title rp-small">{clip(label(item.backend_id), 22)}</text>
                <circle cx={X.targetR - 18} cy={y} r={3.5} className={`rp-dot-status rp-tone-${itemTone}`} />
                <text x={X.targetR - 30} y={y + 4} textAnchor="end" className={`rp-status rp-tone-text-${itemTone}`}>{clip(status(item), 16)}</text>
              </> : <>
                <text x={X.targetL + 46} y={y - 3} className="rp-title">{clip(label(item.backend_id), 28)}</text>
                <circle cx={X.targetL + 49} cy={y + 12} r={3} className={`rp-dot-status rp-tone-${itemTone}`} />
                <text x={X.targetL + 58} y={y + 16} className={`rp-status rp-tone-text-${itemTone}`}>{clip(status(item), 30)}</text>
              </>}
            </g>
          </g>
        })}

        <circle cx={X.hub} cy={hubY} r={HUB_RING} className={`rp-hub-ring rp-tone-${hubTone}`} />
        <circle cx={X.hub} cy={hubY} r={13} className={`rp-hub rp-tone-${hubTone}`} />
        <text x={X.hub} y={hubY + 40} textAnchor="middle" className="rp-title">{t.poolHub}</text>
        {mode && <text x={X.hub} y={hubY + 56} textAnchor="middle" className="rp-sub">{t[mode]}</text>}
      </svg>
    </div>

    <div className="rp-caption" data-outcome={bound ? result.outcome : "idle"}>
      <div className="rp-steps" role="group" aria-label={t.previewTitle}>
        {PHASES.map((candidate, index) =>
          <button
            key={candidate}
            type="button"
            className={phase === candidate ? "active" : ""}
            aria-pressed={phase === candidate}
            aria-label={t[PHASE_LABEL[candidate]]}
            title={t[PHASE_LABEL[candidate]]}
            disabled={!failing.size && index > 0}
            onClick={() => { setPhaseIndex(index); setPlaying(false) }}
          />)}
      </div>
      <div className="rp-caption-body">
        <div className="rp-caption-head">
          <b>{t[PHASE_LABEL[phase]]}</b>
          {bound && draft.members.length > 0 && <span className="rp-outcome" data-outcome={result.outcome}>{t[OUTCOME_LABEL[result.outcome]]}</span>}
        </div>
        <ul>{lines.map((line, index) =>
          <li key={`${line.key}-${index}`} data-tone={line.tone}>{fill(t[line.key], line.values, locale)}</li>)}</ul>
      </div>
    </div>
  </section>
}

function hexagon(cx: number, cy: number, radius: number) {
  return Array.from({ length: 6 }, (_, index) => {
    const angle = Math.PI / 3 * index - Math.PI / 2
    return `${(cx + radius * Math.cos(angle)).toFixed(1)},${(cy + radius * Math.sin(angle)).toFixed(1)}`
  }).join(" ")
}

function FlowDot({ path, begin, tone, seconds = DOT_SECONDS }: { path: string; begin: number; tone: "request" | "error"; seconds?: number }) {
  return <g className="rp-flow-dot" data-tone={tone}>
    <circle r={6} className="rp-flow-halo" />
    <circle r={3} />
    <animateMotion dur={`${seconds}s`} begin={`${begin.toFixed(2)}s`} repeatCount="indefinite" path={path} />
  </g>
}
