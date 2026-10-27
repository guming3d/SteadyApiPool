// Guidance around the pool editor for people new to LiteLLM: a readiness checklist, a glossary
// and a review of what Publish is about to change.
import { BookOpen, CheckCircle2, ChevronDown, CircleAlert, CircleDashed, LoaderCircle, Send } from "lucide-react"

import { fill, useI18n, type Locale, type LitellmMessages } from "../i18n"
import { draftSettings } from "./pool-breaker"
import { MAX_MEMBERS, MIN_MEMBERS } from "../pool-modes"
import type { PoolChange } from "./pool-changes"
import type { ModelCheck } from "../types"
import type { BackendView, PoolWrite } from "./types"
import { Button, Dialog, ErrorText } from "../components/ui"

type CheckState = "ok" | "warn" | "todo"
type Messages = LitellmMessages

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
  const { t, locale } = useI18n("litellm")
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
  creating,
  dirty,
  modelCheck,
  identityReady,
}: {
  draft: PoolWrite
  backendById: Map<string, BackendView>
  creating: boolean
  dirty: boolean
  /** Undefined while the check is loading. */
  modelCheck: ModelCheck | undefined
  /** The gateway has an identity to call managed-identity members with. */
  identityReady: boolean
}, t: Messages, locale: Locale): CheckItem[] {
  const backendOf = (backendId: string) => backendById.get(backendId.toLowerCase())
  const settings = draftSettings(draft)
  const count = draft.members.length
  const usesIdentity = draft.members.some((member) => backendOf(member.backend_id)?.auth_type === "managed_identity")
  const priorities = new Set(draft.members.map((member) => member.priority)).size
  // A failure is answered elsewhere within the request: by a retry, or by the next priority.
  const rescued = settings.retries > 0 || priorities > 1

  return [
    { label: t.checkMembers, state: count >= MIN_MEMBERS && count <= MAX_MEMBERS ? "ok" : "warn" },
    {
      label: t.checkSameModel,
      state: !modelCheck || !count ? "todo" : modelCheck.ok ? "ok" : "warn",
      hint: modelCheck ? modelCheckProblem(modelCheck, t, locale) ?? undefined : undefined,
    },
    {
      label: t.checkBreakers,
      state: count ? "ok" : "todo",
      hint: count ? fill(t.checkBreakersHint, {
        count: settings.circuit_breaker.allowed_fails + 1,
        cooldown: settings.circuit_breaker.cooldown_seconds,
      }, locale) : undefined,
    },
    {
      label: t.checkRetry,
      state: !count ? "todo" : rescued ? "ok" : "warn",
      hint: count && !rescued ? t.checkRetryHint : undefined,
    },
    ...usesIdentity ? [{
      label: t.checkIdentity,
      state: identityReady ? "ok" as const : "warn" as const,
      hint: identityReady ? undefined : t.foundryNoIdentity,
    }] : [],
    { label: t.checkPublished, state: !creating && !dirty ? "ok" : "todo" },
  ]
}

/** One badge for the whole checklist: green when everything passes, amber when something is wrong. */
export function ChecklistBadge({ items, open, onToggle }: { items: CheckItem[]; open: boolean; onToggle: () => void }) {
  const { t } = useI18n("litellm")
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
  const { t } = useI18n("litellm")
  return <div className="checklist" aria-label={t.checklistTitle}>
    <ul>{items.map((item) => <li key={item.label} data-state={item.state}>
      {item.state === "ok" ? <CheckCircle2 size={14} /> : item.state === "warn" ? <CircleAlert size={14} /> : <CircleDashed size={14} />}
      <span>{item.label}{item.hint && <small>{item.hint}</small>}</span>
    </li>)}</ul>
  </div>
}

export function GlossaryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n("litellm")
  return <Dialog open={open} onClose={onClose} title={<span className="dialog-title-icon"><BookOpen size={16} />{t.glossaryTitle}</span>}>
    <dl className="glossary">
      {([
        "glossBackend", "glossPool", "glossBinding", "glossPriority",
        "glossWeight", "glossBreaker", "glossRetry", "glossKey", "glossAffinity",
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
  endpoint,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  open: boolean
  changes: PoolChange[]
  /** Base URL and model name clients call the pool with. */
  endpoint: { baseUrl: string; model: string }
  busy: boolean
  error: unknown
  onConfirm: () => void
  onClose: () => void
}) {
  const { t } = useI18n("litellm")
  const text = (change: PoolChange) => {
    switch (change.kind) {
      case "created": return fill(t.changeCreated, { mode: t[change.mode], count: change.count })
      case "mode": return fill(t.changeMode, { from: t[change.from], to: t[change.to] })
      case "added": return fill(t.changeAdded, { id: change.id, role: t[change.role], share: change.share })
      case "removed": return fill(t.changeRemoved, { id: change.id })
      case "role": return fill(t.changeRole, { id: change.id, from: t[change.from], to: t[change.to] })
      case "share": return fill(t.changeShare, { id: change.id, from: change.from, to: change.to })
      case "title": return fill(t.changeTitle, { title: change.to ?? "—" })
      case "settings": return fill(t.changeSettings, { count: change.count })
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
      </ul>
      : <p className="muted">{t.noChanges}</p>}
    {changes.some((change) => change.kind === "removed") && <p className="muted">{t.publishCleanup}</p>}
    <div className="state">
      <CircleAlert size={14} />
      {fill(t.publishAffects, { url: endpoint.baseUrl, model: endpoint.model })}
    </div>
    <ErrorText error={error} />
  </Dialog>
}
