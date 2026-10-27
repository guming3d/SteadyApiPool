// Guidance around the pool editor for people new to APIM: a readiness checklist, a glossary
// and a review of what Publish is about to change.
import { BookOpen, CheckCircle2, ChevronDown, CircleAlert, CircleDashed, LoaderCircle, Send } from "lucide-react"

import { fill, useI18n, type Locale, type ApimMessages } from "../i18n"
import { MAX_MEMBERS, MIN_MEMBERS } from "../pool-modes"
import type { PoolChange } from "./pool-changes"
import { breakerCatches } from "./routing-sim"
import type { ModelCheck } from "../types"
import type { ApiView, BackendView, PoolWrite } from "./types"
import { Button, Dialog, ErrorText } from "../components/ui"

type CheckState = "ok" | "warn" | "todo"
type Messages = ApimMessages

/** Why the server would refuse to save these members, or null when the model check passes. */
export function modelCheckProblem(check: ModelCheck, t: Messages, locale: Locale) {
  const unverified = check.members.filter((member) => member.problem)
  if (unverified.length) {
    return fill(t.modelUnverified, {
      names: unverified.map((member) => member.backend_id),
      detail: unverified.map((member) => member.problem ?? "").join("; "),
    }, locale)
  }
  if (!check.ok) return fill(t.modelMixed, { models: check.model_names }, locale)
  return null
}

/** Shown under the member list: the refusal reason, or a softer note when versions differ. */
export function ModelCheckNotice({ check, loading }: { check: ModelCheck | undefined; loading: boolean }) {
  const { t, locale } = useI18n("apim")
  if (loading) return <div className="state"><LoaderCircle className="spin" size={14} />{t.modelChecking}</div>
  if (!check) return null
  const problem = modelCheckProblem(check, t, locale)
  if (problem) return <div className="state error" role="alert"><CircleAlert size={14} />{problem}</div>
  const versions = [...new Set(check.members.flatMap((member) =>
    member.model_name && member.model_version ? [`${member.model_name} ${member.model_version}`] : []))]
  if (versions.length > 1) {
    return <div className="state warning"><CircleAlert size={14} />{fill(t.modelVersions, { versions }, locale)}</div>
  }
  return null
}

export type CheckItem = { label: string; state: CheckState; hint?: string }

export function checklistItems({
  draft,
  backendById,
  boundApis,
  creating,
  dirty,
  modelCheck,
}: {
  draft: PoolWrite
  backendById: Map<string, BackendView>
  boundApis: ApiView[]
  creating: boolean
  dirty: boolean
  /** Undefined while the check is loading. */
  modelCheck: ModelCheck | undefined
}, t: Messages, locale: Locale): CheckItem[] {
  const backendOf = (backendId: string) => backendById.get(backendId.toLowerCase())
  const withoutBreaker = draft.members.filter((member) => !backendOf(member.backend_id)?.has_circuit_breaker)
  const missing429 = draft.members.filter((member) => {
    const breaker = backendOf(member.backend_id)?.circuit_breaker
    return breaker && !breakerCatches(breaker, "throttle")
  })
  const withoutRetry = boundApis.filter((item) => !item.binding?.retry_on_429)
  const count = draft.members.length

  return [
    { label: t.checkMembers, state: count >= MIN_MEMBERS && count <= MAX_MEMBERS ? "ok" : "warn" },
    {
      label: t.checkSameModel,
      state: !modelCheck || !count ? "todo" : modelCheck.ok ? "ok" : "warn",
      hint: modelCheck ? modelCheckProblem(modelCheck, t, locale) ?? undefined : undefined,
    },
    {
      label: t.checkBreakers,
      state: count && !withoutBreaker.length ? "ok" : "warn",
      hint: withoutBreaker.length ? fill(t.checkBreakersHint, { names: withoutBreaker.map((member) => member.backend_id) }, locale) : undefined,
    },
    {
      label: t.checkBreakers429,
      state: count && !missing429.length && !withoutBreaker.length ? "ok" : missing429.length ? "warn" : "todo",
      hint: missing429.length ? fill(t.checkBreakers429Hint, { names: missing429.map((member) => member.backend_id) }, locale) : undefined,
    },
    {
      label: t.checkBound,
      state: boundApis.length ? "ok" : "todo",
      hint: boundApis.length ? undefined : t.checkBoundHint,
    },
    {
      label: t.checkRetry,
      state: !boundApis.length ? "todo" : withoutRetry.length ? "warn" : "ok",
      hint: withoutRetry.length ? fill(t.checkRetryHint, { names: withoutRetry.map((item) => item.display_name) }, locale) : undefined,
    },
    { label: t.checkPublished, state: !creating && !dirty ? "ok" : "todo" },
  ]
}

/** One badge for the whole checklist: green when everything passes, amber when something is wrong. */
export function ChecklistBadge({ items, open, onToggle }: { items: CheckItem[]; open: boolean; onToggle: () => void }) {
  const { t } = useI18n("apim")
  const warnings = items.filter((item) => item.state === "warn").length
  const done = items.filter((item) => item.state === "ok").length
  const tone = warnings ? "warn" : done === items.length ? "ok" : "todo"
  // All passed is the usual state, so the green icon alone says it; the words stay in the tooltip.
  const allPassed = tone === "ok" ? t.checksAllPassed : undefined
  return <button type="button" className="check-badge" data-tone={tone} aria-expanded={open} aria-label={allPassed} title={allPassed} onClick={onToggle}>
    {tone === "ok" ? <CheckCircle2 size={13} /> : tone === "warn" ? <CircleAlert size={13} /> : <CircleDashed size={13} />}
    {tone === "ok" ? null
      : warnings ? fill(t.checksAttention, { count: warnings })
      : fill(t.checksPassed, { done, total: items.length })}
    <ChevronDown size={13} className="chevron" />
  </button>
}

export function PoolChecklist({ items }: { items: CheckItem[] }) {
  const { t } = useI18n("apim")
  return <div className="checklist" aria-label={t.checklistTitle}>
    <ul>{items.map((item) => <li key={item.label} data-state={item.state}>
      {item.state === "ok" ? <CheckCircle2 size={14} /> : item.state === "warn" ? <CircleAlert size={14} /> : <CircleDashed size={14} />}
      <span>{item.label}{item.hint && <small>{item.hint}</small>}</span>
    </li>)}</ul>
  </div>
}

export function GlossaryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n("apim")
  return <Dialog open={open} onClose={onClose} title={<span className="dialog-title-icon"><BookOpen size={16} />{t.glossaryTitle}</span>}>
    <dl className="glossary">
      {([
        "glossBackend", "glossPool", "glossBinding", "glossPriority",
        "glossWeight", "glossBreaker", "glossRetry", "glossAffinity",
      ] as const).map((key) => {
        const [term, ...rest] = t[key].split(/[：:]/)
        return <div key={key}><dt>{term}</dt><dd>{rest.join(":")}</dd></div>
      })}
    </dl>
  </Dialog>
}

export function PublishDialog({
  open,
  changes,
  createApiUrl,
  boundApis,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean
  changes: PoolChange[]
  /** Publishing a new pool also creates and binds an API at this URL. */
  createApiUrl?: string
  boundApis: ApiView[]
  busy: boolean
  error: unknown
  onConfirm: () => void
  onClose: () => void
}) {
  const { t, locale } = useI18n("apim")
  const text = (change: PoolChange) => {
    switch (change.kind) {
      case "created": return fill(t.changeCreated, { mode: t[change.mode], count: change.count })
      case "mode": return fill(t.changeMode, { from: t[change.from], to: t[change.to] })
      case "added": return fill(t.changeAdded, { id: change.id, role: t[change.role], share: change.share })
      case "removed": return fill(t.changeRemoved, { id: change.id })
      case "role": return fill(t.changeRole, { id: change.id, from: t[change.from], to: t[change.to] })
      case "share": return fill(t.changeShare, { id: change.id, from: change.from, to: change.to })
      case "affinity": return change.to ? t.changeAffinityOn : t.changeAffinityOff
      case "title": return fill(t.changeTitle, { title: change.to ?? "—" })
      case "breaker": return fill(t.changeBreaker, { count: change.count })
    }
  }
  return <Dialog
    open={open}
    onClose={() => { if (!busy) onClose() }}
    title={t.publishReview}
    description={t.publishReviewHint}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={busy} onClick={onConfirm}>
        {busy ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{busy ? t.publishing : t.publish}
      </Button>
    </>}
  >
    {changes.length
      ? <ul className="change-list">
        {changes.map((change, index) => <li key={index} data-kind={change.kind}>{text(change)}</li>)}
        {createApiUrl && <li data-kind="added">{fill(t.changeCreateApi, { url: createApiUrl })}</li>}
      </ul>
      : <p className="muted">{t.noChanges}</p>}
    {changes.some((change) => change.kind === "removed") && <p className="muted">{t.publishCleanup}</p>}
    <div className={`state ${boundApis.length ? "warning" : ""}`}>
      <CircleAlert size={14} />
      {boundApis.length
        ? fill(t.publishAffects, { apis: boundApis.map((item) => `${item.display_name} (/${item.path})`) }, locale)
        : t.publishNotBound}
    </div>
    <ErrorText error={error} />
  </Dialog>
}
