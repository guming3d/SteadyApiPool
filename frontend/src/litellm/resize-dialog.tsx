// Resize the running gateway for new traffic, from the gateway panel. The backend reads the
// gateway and lists what the plan would change (litellm/resize.py); nothing changes until the
// user has seen that list and the warning that production traffic may be affected, and confirmed.
import { CheckCircle2, CircleAlert, LoaderCircle, TriangleAlert } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, queries } from "./api"
import { CodeBlock } from "./call-guide"
import { PlanSteps, SizingFields, useSettled } from "./capacity-sizer"
import { fill, useI18n } from "../i18n"
import { DEFAULT_SIZING, noteKey, noteValues, planSummary, resizeChangesSomething, resizeImpacts } from "./sizing-view"
import type { GatewaySizing, ResizeResult } from "./types"
import { Button, Dialog, ErrorText } from "../components/ui"

export function ResizeDialog({ initial, onClose }: { initial: GatewaySizing | null; onClose: () => void }) {
  const { t, locale } = useI18n("litellm")
  const queryClient = useQueryClient()
  const [sizing, setSizing] = useState<GatewaySizing>(initial ?? DEFAULT_SIZING)
  const [database, setDatabase] = useState(false)
  const [understood, setUnderstood] = useState(false)
  const [result, setResult] = useState<ResizeResult | null>(null)
  const settled = useSettled(sizing)
  const preview = useQuery({ ...queries.resizePlan(settled), enabled: settled !== null && result === null })
  const command = useQuery(queries.resizeCommand(result?.command_id ?? null))
  const apply = useMutation({
    mutationFn: () => api.resize({ sizing: settled!, etag: preview.data!.etag, database }),
    onSuccess: (done) => {
      setResult(done)
      // The gateway's size, tags and price changed.
      void queryClient.invalidateQueries({ queryKey: ["litellm"] })
    },
  })
  const change = (next: GatewaySizing) => {
    setSizing(next)
    setUnderstood(false)
  }

  if (result) {
    const state = command.data?.state
    return <Dialog open onClose={onClose} title={t.resizeTitle} footer={<Button variant="primary" onClick={onClose}>{t.close}</Button>}>
      <div className="resize-dialog">
        <div className="state" data-status="active"><CheckCircle2 size={14} />{result.applied.length
          ? fill(t.resizeDone, { items: result.applied.map((item) => t[`resizeItem_${item}`]).join(", ") })
          : t.resizeNothingDone}</div>
        {result.command_id && (state === "failed"
          ? <div className="callout warning"><CircleAlert size={15} /><div>
            <span>{t.resizeCommandFailed}</span>
            {command.data?.log_tail && <pre className="resize-log">{command.data.log_tail}</pre>}
          </div></div>
          : state === "succeeded"
            ? <div className="state" data-status="active"><CheckCircle2 size={14} />{t.resizeCommandDone}</div>
            : <div className="state"><LoaderCircle className="spin" size={14} />{t.resizeCommandRunning}</div>)}
        <ErrorText error={command.error} />
      </div>
    </Dialog>
  }

  const plan = preview.data
  const fresh = Boolean(plan) && !preview.isPlaceholderData && settled !== null && !preview.isFetching
  const changes = plan?.changes ?? []
  const applied = changes.filter((item) => item.action === "apply")
  const optional = changes.filter((item) => item.action === "optional")
  const manual = changes.filter((item) => item.action === "manual")
  const impacts = resizeImpacts(changes, database)
  const blocked = Boolean(plan?.plan.problems.length)
  const something = resizeChangesSomething(changes, database)
  const ready = fresh && !blocked && something && understood && !apply.isPending
  const summary = plan && planSummary(plan.plan, locale)
  return <Dialog
    open
    wide
    onClose={onClose}
    title={t.resizeTitle}
    description={t.resizeHint}
    footer={<>
      <Button onClick={onClose} disabled={apply.isPending}>{t.cancel}</Button>
      <Button variant="primary" disabled={!ready} onClick={() => apply.mutate()}>
        {apply.isPending && <LoaderCircle className="spin" size={13} />}{t.resizeApply}
      </Button>
    </>}
  >
    <div className="resize-dialog">
      <SizingFields sizing={sizing} onChange={change} />
      {preview.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.resizeReading}</div>}
      <ErrorText error={preview.error} />
      {plan && summary && settled && <>
        <b className="sizer-summary">{fill(plan.platform === "aks" ? t.planSummaryAks : t.planSummaryAca, {
          ...summary,
          redis: summary.redis ? fill(t.planRedis, { sku: summary.redis }) : t.planNoRedis,
        })}</b>
        {blocked && <div className="callout warning"><CircleAlert size={15} /><div>
          <span>{t.resizeBlocked}</span>
          <ul>{plan.plan.problems.map((note) => <li key={note.code}>{fill(t[noteKey(note)], noteValues(note, locale))}</li>)}</ul>
        </div></div>}
        <PlanSteps plan={plan.plan} sizing={settled} />
        {changes.length === 0
          ? <div className="state" data-status="active"><CheckCircle2 size={14} />{t.resizeNoChanges}</div>
          : <>
            {(applied.length > 0 || optional.length > 0) && <table className="resize-changes">
              <thead><tr><th>{t.resizeColumnItem}</th><th>{t.resizeColumnNow}</th><th>{t.resizeColumnNew}</th></tr></thead>
              <tbody>{[...applied, ...optional].map((item) => <tr key={item.item}>
                <td>{t[`resizeItem_${item.item}`]}</td><td>{item.current}</td><td><b>{item.proposed}</b></td>
              </tr>)}</tbody>
            </table>}
            {optional.some((item) => item.item === "database") && <label className="check">
              <input type="checkbox" checked={database} onChange={(event) => { setDatabase(event.target.checked); setUnderstood(false) }} />
              <span className="check-copy">{t.resizeDatabase}<small>{t.resizeImpact_database}</small></span>
            </label>}
            {manual.length > 0 && <div className="resize-manual">
              <b>{t.resizeManualTitle}</b>
              {manual.map((item) => <div key={item.item}>
                <span>{fill(t.resizeManualItem, { item: t[`resizeItem_${item.item}`], current: item.current, proposed: item.proposed })}</span>
                {item.command
                  ? <CodeBlock code={item.command} label={t[`resizeItem_${item.item}`]} />
                  : <small className="muted">{t[`resizeManual_${item.item === "timeout" ? "timeout" : "redis"}`]}</small>}
              </div>)}
            </div>}
          </>}
        {something && !blocked && <div className="callout warning"><TriangleAlert size={15} /><div>
          <b>{t.resizeWarning}</b>
          <ul>{impacts.map((key) => <li key={key}>{t[key]}</li>)}</ul>
          <label className="check"><input type="checkbox" checked={understood} onChange={(event) => setUnderstood(event.target.checked)} />{t.resizeUnderstood}</label>
        </div></div>}
      </>}
      <ErrorText error={apply.error} />
    </div>
  </Dialog>
}
