// The pool's circuit breaker rule, in the pool's settings. Publishing writes it to every member
// this app manages; see pool-breaker.ts.
import { CircleAlert, RotateCcw, ShieldCheck } from "lucide-react"

import { fill, useI18n } from "../i18n"
import { DEFAULT_BREAKER, sameBreaker } from "../pool-breaker"
import type { CircuitBreaker } from "../types"
import { Button, Field } from "./ui"

export function PoolBreakerSettings({
  rule,
  uniform,
  edited,
  disabled,
  shared,
  readOnlyIds,
  onChange,
  onReset,
}: {
  rule: CircuitBreaker
  /** The managed members all have this rule today. */
  uniform: boolean
  /** The rule differs from what the published members have. */
  edited: boolean
  disabled: boolean
  /** Members whose rule changes on publish and that other pools use too. */
  shared: Array<{ id: string; pools: string[] }>
  /** Members this app didn't create: they keep their own rule. */
  readOnlyIds: string[]
  onChange: (rule: CircuitBreaker) => void
  onReset: () => void
}) {
  const { t, locale } = useI18n()
  const number = (key: "failure_count" | "interval_seconds" | "trip_duration_seconds") =>
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const value = Number(event.target.value)
      if (Number.isInteger(value) && value >= 1) onChange({ ...rule, [key]: value })
    }
  const signals = [
    ...rule.status_code_ranges.map((range) => range.minimum === range.maximum ? `${range.minimum}` : `${range.minimum}-${range.maximum}`),
    ...rule.error_reasons,
  ].join(" · ")

  return <div className="pool-breaker">
    <div className="pool-breaker-head">
      <ShieldCheck size={14} />
      <span><b>{t.circuitBreaker}</b><small>{t.poolBreakerHint}</small></span>
      <span className="pool-breaker-actions">
        {edited && <Button variant="ghost" disabled={disabled} onClick={onReset}><RotateCcw size={13} />{t.undo}</Button>}
        {!sameBreaker(rule, DEFAULT_BREAKER) && <Button variant="ghost" disabled={disabled} onClick={() => onChange(DEFAULT_BREAKER)}>{t.breakerUseDefault}</Button>}
      </span>
    </div>
    <div className="form-grid pool-breaker-fields">
      <Field label={t.failureCount}><input type="number" min={1} value={rule.failure_count} disabled={disabled} onChange={number("failure_count")} /></Field>
      <Field label={t.interval}><input type="number" min={1} value={rule.interval_seconds} disabled={disabled} onChange={number("interval_seconds")} /></Field>
      <Field label={t.tripDuration}><input type="number" min={1} value={rule.trip_duration_seconds} disabled={disabled} onChange={number("trip_duration_seconds")} /></Field>
      <label className="check"><input type="checkbox" checked={rule.accept_retry_after} disabled={disabled} onChange={(event) => onChange({ ...rule, accept_retry_after: event.target.checked })} />{t.acceptRetryAfter}</label>
      <div className="state span-2">{t.failureSignals}: {signals}</div>
    </div>
    {!uniform && <div className="state warning"><CircleAlert size={14} />{t.breakerMixed}</div>}
    {shared.length > 0 && <div className="state warning"><CircleAlert size={14} />
      {fill(t.breakerShared, { names: shared.map((item) => item.id), pools: [...new Set(shared.flatMap((item) => item.pools))] }, locale)}
    </div>}
    {readOnlyIds.length > 0 && <div className="state"><CircleAlert size={14} />{fill(t.breakerReadOnly, { names: readOnlyIds }, locale)}</div>}
  </div>
}
