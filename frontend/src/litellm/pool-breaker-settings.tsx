// The pool's resilience settings in its Settings tab: LiteLLM's cooldown (circuit breaker),
// retries and the per-attempt timeout. Publishing writes them to every member row; see
// pool-breaker.ts.
import { Info, RotateCcw, ShieldCheck } from "lucide-react"

import { fill, useI18n } from "../i18n"
import { DEFAULT_SETTINGS, failuresToCool, sameSettings, type PoolSettings } from "./pool-breaker"
import { Button, Field } from "../components/ui"

export function PoolBreakerSettings({
  settings,
  edited,
  disabled,
  onChange,
  onReset,
}: {
  settings: PoolSettings
  /** The settings differ from what the published pool has. */
  edited: boolean
  disabled: boolean
  onChange: (settings: PoolSettings) => void
  onReset: () => void
}) {
  const { t } = useI18n("litellm")
  const breaker = settings.circuit_breaker
  const integer = (min: number, max: number, apply: (value: number) => PoolSettings) =>
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const value = Number(event.target.value)
      if (Number.isInteger(value) && value >= min && value <= max) onChange(apply(value))
    }

  return <div className="pool-breaker">
    <div className="pool-breaker-head">
      <ShieldCheck size={14} />
      <span><b>{t.circuitBreaker}</b><small>{t.poolBreakerHint}</small></span>
      <span className="pool-breaker-actions">
        {edited && <Button variant="ghost" disabled={disabled} onClick={onReset}><RotateCcw size={13} />{t.undo}</Button>}
        {!sameSettings(settings, DEFAULT_SETTINGS) && <Button variant="ghost" disabled={disabled} onClick={() => onChange(DEFAULT_SETTINGS)}>{t.breakerUseDefault}</Button>}
      </span>
    </div>
    <div className="form-grid pool-breaker-fields">
      <Field label={t.allowedFails} hint={fill(t.allowedFailsHint, { count: failuresToCool(breaker) })}>
        <input type="number" min={0} max={1000} value={breaker.allowed_fails} disabled={disabled}
          onChange={integer(0, 1000, (value) => ({ ...settings, circuit_breaker: { ...breaker, allowed_fails: value } }))} />
      </Field>
      <Field label={t.cooldown}>
        <input type="number" min={1} max={86_400} value={breaker.cooldown_seconds} disabled={disabled}
          onChange={integer(1, 86_400, (value) => ({ ...settings, circuit_breaker: { ...breaker, cooldown_seconds: value } }))} />
      </Field>
      <Field label={t.retries} hint={t.retriesHint}>
        <input type="number" min={0} max={10} value={settings.retries} disabled={disabled}
          onChange={integer(0, 10, (value) => ({ ...settings, retries: value }))} />
      </Field>
      <Field label={t.attemptTimeout} hint={t.attemptTimeoutHint}>
        <input type="number" min={1} max={600} value={settings.timeout_seconds} disabled={disabled}
          onChange={integer(1, 600, (value) => ({ ...settings, timeout_seconds: value }))} />
      </Field>
    </div>
    <div className="state"><Info size={14} />{t.sessionPinningNote}</div>
  </div>
}
