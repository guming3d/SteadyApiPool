// The pool's resilience settings in its Settings tab: the alias's failure rule (which answers
// count as a failure, how long a failed member stays out, retries on the same member) and the
// retry limit across members. Publishing writes them to the alias; see pool-breaker.ts.
import { Info, RotateCcw, ShieldCheck } from "lucide-react"
import { useEffect, useState } from "react"

import { fill, useI18n } from "../i18n"
import { DEFAULT_SETTINGS, MAX_EXCLUDE_SECONDS, MAX_RETRIES, parseRanges, rangesText, sameSettings, type PoolSettings } from "./pool-breaker"
import { Button, Field } from "../components/ui"

export function PoolBreakerSettings({
  settings,
  edited,
  disabled,
  customFailures,
  policies,
  onChange,
  onReset,
}: {
  settings: PoolSettings
  /** The settings differ from what the published pool has. */
  edited: boolean
  disabled: boolean
  /** The alias has failure rules this app didn't write; publishing replaces them. */
  customFailures: boolean
  /** Inline policies on the alias, kept as they are. */
  policies: string[]
  onChange: (settings: PoolSettings) => void
  onReset: () => void
}) {
  const { t, locale } = useI18n("aigateway")
  const breaker = settings.circuit_breaker
  const shown = rangesText(breaker.status_code_ranges)
  // Typed freely; applied once it parses.
  const [codes, setCodes] = useState(shown)
  useEffect(() => setCodes(shown), [shown])
  const parsed = parseRanges(codes)
  const integer = (min: number, max: number, apply: (value: number) => PoolSettings) =>
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const value = Number(event.target.value)
      if (Number.isInteger(value) && value >= min && value <= max) onChange(apply(value))
    }
  const changeCodes = (text: string) => {
    setCodes(text)
    const ranges = parseRanges(text)
    if (ranges && (ranges.length || breaker.no_response)) onChange({ ...settings, circuit_breaker: { ...breaker, status_code_ranges: ranges } })
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
    {customFailures && <div className="state warning"><Info size={14} />{t.customFailuresNote}</div>}
    <div className="form-grid pool-breaker-fields">
      <Field label={t.failureCodes} hint={parsed ? t.failureCodesHint : t.failureCodesInvalid}>
        <input value={codes} disabled={disabled} spellCheck={false} aria-invalid={!parsed} onChange={(event) => changeCodes(event.target.value)} />
      </Field>
      <label className="check">
        <input type="checkbox" checked={breaker.no_response} disabled={disabled || (!breaker.status_code_ranges.length && breaker.no_response)}
          onChange={(event) => onChange({ ...settings, circuit_breaker: { ...breaker, no_response: event.target.checked } })} />
        {t.failureNoResponse}
      </label>
      <Field label={t.excludeFor} hint={breaker.exclude_seconds ? fill(t.excludeForHint, { seconds: breaker.exclude_seconds }, locale) : t.excludeForZero}>
        <input type="number" min={0} max={MAX_EXCLUDE_SECONDS} value={breaker.exclude_seconds} disabled={disabled}
          onChange={integer(0, MAX_EXCLUDE_SECONDS, (value) => ({ ...settings, circuit_breaker: { ...breaker, exclude_seconds: value } }))} />
      </Field>
      <Field label={t.sameMemberRetries} hint={t.sameMemberRetriesHint}>
        <input type="number" min={0} max={10} value={breaker.same_member_retries} disabled={disabled}
          onChange={integer(0, 10, (value) => ({ ...settings, circuit_breaker: { ...breaker, same_member_retries: value } }))} />
      </Field>
      <Field label={t.retries} hint={settings.retries ? t.retriesHint : t.retriesZero}>
        <input type="number" min={0} max={MAX_RETRIES} value={settings.retries} disabled={disabled}
          onChange={integer(0, MAX_RETRIES, (value) => ({ ...settings, retries: value }))} />
      </Field>
    </div>
    {policies.length > 0 && <div className="state"><Info size={14} />{fill(t.policiesKept, { policies }, locale)}</div>}
    <div className="state"><Info size={14} />{t.aliasRoutingNote}</div>
  </div>
}
