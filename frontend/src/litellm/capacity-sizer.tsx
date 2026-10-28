// The capacity sizer, the first step of Deploy a gateway: the user describes the traffic at its
// peak, and the backend's plans (litellm/sizing.py) say where the gateway should run and how big
// it has to be, with every step of the arithmetic. The numbers are the backend's; this only
// words them (sizing-view.ts).
import { CircleAlert, Cloud, Download, Info, LoaderCircle, Server } from "lucide-react"
import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "./api"
import { fill, useI18n } from "../i18n"
import { Button, ErrorText, Field } from "../components/ui"
import { LIMITS, MODEL_SPEEDS, formatTpm, invalidFields, noteKey, noteValues, parseCount, planSteps, planSummary } from "./sizing-view"
import type { GatewayPlan, GatewayPlatform, GatewaySizing, ModelSpeed } from "./types"

type CountField = keyof typeof LIMITS

/** The scenario once the user stops typing; null while a field is out of bounds. */
export function useSettled(sizing: GatewaySizing): GatewaySizing | null {
  const invalid = invalidFields(sizing).length > 0
  const [settled, setSettled] = useState<GatewaySizing | null>(invalid ? null : sizing)
  useEffect(() => {
    if (invalid) return
    const timer = setTimeout(() => setSettled(sizing), 300)
    return () => clearTimeout(timer)
  }, [sizing, invalid])
  return invalid ? null : settled
}

/** The sizer's questions: the traffic at its peak, and More options. */
export function SizingFields({ sizing, onChange }: { sizing: GatewaySizing; onChange: (sizing: GatewaySizing) => void }) {
  const { t, locale } = useI18n("litellm")
  const invalid = invalidFields(sizing)
  const set = (next: Partial<GatewaySizing>) => onChange({ ...sizing, ...next })
  const count = (field: CountField, label: string, hint?: string) => <Field label={label} hint={hint}>
    <input
      type="number"
      min={LIMITS[field].min}
      max={LIMITS[field].max}
      value={Number.isNaN(sizing[field]) ? "" : sizing[field]}
      aria-invalid={invalid.includes(field)}
      onChange={(event) => set({ [field]: parseCount(event.target.value) })}
    />
  </Field>
  const rps = sizing.peak_rpm / 60
  const tpm = sizing.peak_rpm * (sizing.input_tokens + sizing.output_tokens)
  return <>
    <div className="form-grid">
      {count("peak_rpm", t.sizerPeakRpm, Number.isFinite(rps) ? fill(t.sizerRps, { rps: rps.toLocaleString(locale, { maximumFractionDigits: rps < 10 ? 1 : 0 }) }) : undefined)}
      {count("timeout_seconds", t.sizerTimeout, t.sizerTimeoutHint)}
      {count("input_tokens", t.sizerInputTokens, Number.isFinite(tpm) ? fill(t.sizerTpm, { tpm: formatTpm(tpm, locale) }) : undefined)}
      {count("output_tokens", t.sizerOutputTokens)}
      <label className="check span-2"><input type="checkbox" checked={sizing.streaming} onChange={(event) => set({ streaming: event.target.checked })} />
        <span className="check-copy">{t.sizerStreaming}<small>{t.sizerStreamingHint}</small></span>
      </label>
    </div>
    <details className="sizer-more">
      <summary>{t.sizerMore}</summary>
      <div className="form-grid">
        <Field label={t.sizerSpeed}>
          <select value={sizing.model_speed} onChange={(event) => set({ model_speed: event.target.value as ModelSpeed })}>
            {MODEL_SPEEDS.map((speed) => <option key={speed} value={speed}>{t[`sizerSpeed_${speed}`]}</option>)}
          </select>
        </Field>
        <Field label={t.sizerResponse} hint={t.sizerResponseHint}>
          <input
            type="number"
            min={0}
            step={0.5}
            value={sizing.response_seconds ?? ""}
            aria-invalid={invalid.includes("response_seconds")}
            onChange={(event) => set({ response_seconds: event.target.value.trim() ? Number(event.target.value) : null })}
          />
        </Field>
        {count("headroom_percent", t.sizerHeadroom)}
        <span />
        <label className="check span-2"><input type="checkbox" checked={sizing.zone_redundant} onChange={(event) => set({ zone_redundant: event.target.checked })} />{t.sizerZones}</label>
        <label className="check span-2"><input type="checkbox" checked={sizing.kubernetes_team} onChange={(event) => set({ kubernetes_team: event.target.checked })} />{t.choiceTeam}</label>
        <label className="check span-2"><input type="checkbox" checked={sizing.self_hosted_models} onChange={(event) => set({ self_hosted_models: event.target.checked })} />{t.choiceGpu}</label>
        <label className="check span-2"><input type="checkbox" checked={sizing.private_network} onChange={(event) => set({ private_network: event.target.checked })} />{t.choicePrivate}</label>
      </div>
    </details>
    {invalid.length > 0 && <div className="state">{fill(t.sizerInvalid, { fields: invalid.join(", ") })}</div>}
  </>
}

/** "How we got this": the plan's arithmetic, step by step. */
export function PlanSteps({ plan, sizing }: { plan: GatewayPlan; sizing: GatewaySizing }) {
  const { t, locale } = useI18n("litellm")
  return <details className="sizer-steps">
    <summary>{t.planHow}</summary>
    <ol>{planSteps(plan, sizing, locale).map((step) => <li key={step.key}>{fill(t[step.key], step.values)}</li>)}</ol>
  </details>
}

export function CapacitySizer({ sizing, onChange, onDeploy, onFiles, onCancel }: {
  sizing: GatewaySizing
  onChange: (sizing: GatewaySizing) => void
  onDeploy: (platform: GatewayPlatform, sizing: GatewaySizing) => void
  onFiles: () => void
  onCancel: () => void
}) {
  const { t } = useI18n("litellm")
  const settled = useSettled(sizing)
  const plans = useQuery(queries.plans(settled))

  return <section className="gateway-form gateway-choice">
    <h3>{t.choiceTitle}</h3>
    <p className="muted">{t.choiceHint}</p>
    <SizingFields sizing={sizing} onChange={onChange} />
    {plans.isLoading && settled && <div className="state"><LoaderCircle className="spin" size={14} />{t.sizerPlanning}</div>}
    <ErrorText error={plans.error} />
    {plans.data && settled && <div className="choice-result">
      {(["aca", "aks"] as const).map((platform) => <PlanCard
        key={platform}
        plan={plans.data[platform]}
        sizing={settled}
        recommended={plans.data.recommended === platform}
        onDeploy={() => onDeploy(platform, settled)}
        onFiles={onFiles}
      />)}
    </div>}
    {plans.data && <p className="muted sizer-estimate">{t.planEstimate}</p>}
    <div className="gateway-form-actions"><Button onClick={onCancel}>{t.cancel}</Button></div>
  </section>
}

function PlanCard({ plan, sizing, recommended, onDeploy, onFiles }: {
  plan: GatewayPlan
  sizing: GatewaySizing
  recommended: boolean
  onDeploy: () => void
  onFiles: () => void
}) {
  const { t, locale } = useI18n("litellm")
  const aks = plan.platform === "aks"
  const summary = planSummary(plan, locale)
  const redis = summary.redis ? fill(t.planRedis, { sku: summary.redis }) : t.planNoRedis
  const blocked = plan.problems.length > 0
  return <div className="choice-card" data-recommended={recommended}>
    <header>
      {aks ? <Server size={16} /> : <Cloud size={16} />}
      <b>{aks ? t.choiceAks : t.choiceAca}</b>
      {recommended && <span className="tag" data-tone="ok">{t.choiceRecommended}</span>}
    </header>
    <p className="muted">{aks ? t.choiceAksBody : t.choiceAcaBody}</p>
    <b className="sizer-summary">{fill(aks ? t.planSummaryAks : t.planSummaryAca, { ...summary, redis })}</b>
    {blocked && <div className="callout warning"><CircleAlert size={15} /><div>
      <span>{t.planCantDeploy}</span>
      <ul>{plan.problems.map((note) => <li key={note.code}>{fill(t[noteKey(note)], noteValues(note, locale))}</li>)}</ul>
    </div></div>}
    {plan.reasons.length > 0 && <ul>{plan.reasons.map((note) => <li key={note.code}>{fill(t[noteKey(note)], noteValues(note, locale))}</li>)}</ul>}
    {plan.warnings.length > 0 && <ul className="sizer-warnings">
      {plan.warnings.map((note) => <li key={note.code}><Info size={12} />{fill(t[noteKey(note)], noteValues(note, locale))}</li>)}
    </ul>}
    <PlanSteps plan={plan} sizing={sizing} />
    <div className="choice-actions">
      <Button variant={recommended && !blocked ? "primary" : "outline"} disabled={blocked} onClick={onDeploy}>
        {aks ? t.choiceDeployAks : t.choiceDeployAca}
      </Button>
      {aks && <Button variant="ghost" onClick={onFiles}><Download size={13} />{t.choiceAksFiles}</Button>}
    </div>
  </div>
}
