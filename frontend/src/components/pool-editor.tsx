// The pool editor. Members are APIM Single backends, and saving writes the pool to APIM
// directly.
import { ArrowRight, CheckCircle2, ChevronDown, CircleAlert, CircleHelp, Link2, Link2Off, ListPlus, LoaderCircle, Pencil, RotateCcw, Send, ShieldCheck, Star, Trash2, X } from "lucide-react"
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import {
  addMember,
  appendMembers,
  changeWeight,
  deploymentMode,
  draftIssues,
  MAX_MEMBERS,
  membersForMode,
  poolPayload,
  primaryFirst,
  removeMember,
  setPrimary,
  suggestApiPath,
  trafficShare,
  API_PATH_PATTERN,
  type DeploymentMode,
} from "../pool-modes"
import { followsPool, membersToUpdate, poolBreaker, readOnlyMembers, sameBreaker, withPoolBreaker } from "../pool-breaker"
import { poolChanges } from "../pool-changes"
import type { BackendView, CircuitBreaker, MemberModel, PoolApiCreate, PoolView, PoolWrite } from "../types"
import { ApiBindings, ApiPathField, apiPathConflict, endpointUrl } from "./api-bindings"
import { authLabel, breakerLabel } from "./backend-labels"
import { ChecklistBadge, checklistItems, GlossaryDialog, ModelCheckNotice, PoolChecklist, PublishDialog } from "./pool-guide"
import { CallGuide, CopyButton } from "./call-guide"
import { PoolMonitor } from "./pool-monitor"
import { MemberBackendDialog } from "./member-backend-dialog"
import { AddMembersDialog } from "./member-picker"
import { PoolBreakerSettings } from "./pool-breaker-settings"
import { RoutingPreview } from "./routing-preview"
import { RemovePoolDialog } from "./removal-dialog"
import { Button, ErrorText, Field, FieldHelp } from "./ui"

const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,79}$/

function draftFromPool(pool: PoolView | null): PoolWrite {
  if (!pool) return { members: [], session_affinity: false, title: null, description: null }
  return {
    members: primaryFirst(pool.members),
    session_affinity: pool.session_affinity,
    title: pool.title,
    description: null,
  }
}

const signature = (draft: PoolWrite) => JSON.stringify(draft)

type EditorTab = "members" | "routing" | "connect" | "settings" | "monitor"

export type PoolCreateRequest = { id: string; title: string; members: string[] }
/** Opens an existing pool with these backends added to the draft, e.g. imported deployments. */
export type PoolAddRequest = { poolId: string; members: string[] }

function initialDraft(initial: PoolCreateRequest | undefined): PoolWrite {
  const empty = draftFromPool(null)
  if (!initial) return empty
  // Failover by default: the first imported deployment is primary, the rest are standbys.
  const members = initial.members.reduce(
    (current, backendId) => addMember(current, backendId, "failover"), empty.members)
  return { ...empty, title: initial.title || null, members }
}

export function PoolEditor({
  poolId,
  initial,
  addMembers,
  autoCreateApi,
  onAutoCreateApiStarted,
  onCreated,
  onDeleted,
  header,
}: {
  /** Omit to create a new pool. */
  poolId?: string
  /** Prefills a new pool, e.g. from imported deployments. */
  initial?: PoolCreateRequest
  /** Backends to add to an existing pool's draft once it loads; publishing stays manual. */
  addMembers?: string[]
  /** An API to create and bind once the pool's editor opens, e.g. right after creating it. */
  autoCreateApi?: PoolApiCreate
  onAutoCreateApiStarted?: () => void
  /** `api` is the API the new pool should get, if the operator kept that option on. */
  onCreated?: (poolId: string, api?: PoolApiCreate) => void
  onDeleted?: () => void
  header?: (controls: ReactNode) => ReactNode
}) {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const creating = poolId === undefined
  const me = useQuery(queries.me())
  const capabilities = useQuery(queries.capabilities())
  const backends = useQuery(queries.backends())
  // Other pools sharing a member see its circuit breaker change too.
  const pools = useQuery(queries.pools())
  const pool = useQuery({ ...queries.pool(poolId ?? ""), enabled: !creating })
  // New pools need it too, to flag an API path that is already taken.
  const apis = useQuery(queries.apis())
  const affinitySupported = capabilities.data?.session_affinity_supported === true
  const readOnly = !me.data?.can_write || pool.data?.protected === true

  const baselineRef = useRef(draftFromPool(null))
  const [baseline, setBaseline] = useState(() => signature(baselineRef.current))
  const [draft, setDraft] = useState<PoolWrite>(() => initialDraft(initial))
  const [mode, setMode] = useState<DeploymentMode | null>(initial?.members.length ? "failover" : null)
  const [newId, setNewId] = useState(initial?.id ?? "")
  // A new pool gets its own API by default, at a path suggested from its title until edited.
  const [withApi, setWithApi] = useState(true)
  const [apiPath, setApiPath] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  // A just-created pool opens on Connect, where its API is being created.
  const [tab, setTab] = useState<EditorTab>(autoCreateApi ? "connect" : "members")
  const tabIdPrefix = `pool-tab${useId().replace(/[^A-Za-z0-9]/g, "")}`
  // In failover the role is a radio: exactly one member is primary.
  const primaryGroup = useId()
  const [checksOpen, setChecksOpen] = useState(false)
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  // Lower-case ID of the member whose details are shown.
  const [expanded, setExpanded] = useState<string | null>(null)
  // The pool's circuit breaker as edited here; null follows what the published members share.
  const [breakerEdit, setBreakerEdit] = useState<CircuitBreaker | null>(null)
  // A custom endpoint member whose URL or credentials are being edited.
  const [editingMember, setEditingMember] = useState<BackendView | null>(null)

  useEffect(() => {
    if (!pool.isSuccess) return
    const next = draftFromPool(pool.data)
    const previous = baselineRef.current
    baselineRef.current = next
    setBaseline(signature(next))
    setMode(next.members.length ? deploymentMode(next.members) : null)
    // Keep unsaved edits unless the draft still equals the previous baseline.
    setDraft((current) => signature(current) === signature(previous) ? next : current)
  }, [pool.data, pool.dataUpdatedAt, pool.isSuccess])

  // Applied once, on top of the loaded pool (this effect runs after the baseline one above).
  const pendingAdd = useRef(addMembers)
  const [addedCount, setAddedCount] = useState<number | null>(null)
  useEffect(() => {
    const backendIds = pendingAdd.current
    if (!backendIds?.length || !pool.isSuccess) return
    pendingAdd.current = undefined
    setAddedCount(appendMembers(primaryFirst(pool.data.members), backendIds).added.length)
    setDraft((current) => ({ ...current, members: appendMembers(current.members, backendIds).members }))
  }, [pool.data, pool.isSuccess])

  const save = useMutation({
    mutationFn: () => {
      const id = creating ? newId.trim() : poolId
      return api.savePool(id, {
        ...poolPayload(draft, affinitySupported),
        circuit_breaker: breaker,
        etag: creating ? null : pool.data?.etag ?? null,
      })
    },
    onSuccess: (saved) => {
      // The saved pool becomes both baseline and draft, so the editor is clean afterwards.
      const next = draftFromPool(saved)
      baselineRef.current = next
      setBaseline(signature(next))
      setDraft(next)
      setMode(deploymentMode(next.members))
      setBreakerEdit(null)
      queryClient.setQueryData(keys.pool(saved.id), saved)
      void queryClient.invalidateQueries({ queryKey: keys.pools, exact: true })
      // Members took the pool's rule, and removed ones no pool uses are gone.
      void queryClient.invalidateQueries({ queryKey: keys.backends })
      setNotice(t.saved)
      setPublishOpen(false)
      if (creating) onCreated?.(saved.id, withApi ? { path: newApiPath } : undefined)
    },
  })

  const backendById = useMemo(
    () => new Map((backends.data ?? []).map((backend) => [backend.id.toLowerCase(), backend])),
    [backends.data],
  )
  const selectedIds = new Set(draft.members.map((member) => member.backend_id.toLowerCase()))
  const effectiveIds = new Set(pool.data?.members.map((member) => member.backend_id.toLowerCase()))
  // The pool's circuit breaker: what its published members share, unless edited here.
  const memberIdList = draft.members.map((member) => member.backend_id)
  const derivedBreaker = poolBreaker((creating ? draft.members : baselineRef.current.members).map((member) => member.backend_id), backendById)
  const breaker = breakerEdit ?? derivedBreaker.rule
  const breakerEdited = breakerEdit !== null && !sameBreaker(breakerEdit, derivedBreaker.rule)
  const breakerUpdates = membersToUpdate(memberIdList, backendById, breaker)
  const otherPools = (backendId: string) => (pools.data ?? [])
    .filter((item) => item.id.toLowerCase() !== (poolId ?? "").toLowerCase()
      && item.members.some((member) => member.backend_id.toLowerCase() === backendId.toLowerCase()))
    .map((item) => item.id)
  const breakerShared = breakerUpdates.map((id) => ({ id, pools: otherPools(id) })).filter((item) => item.pools.length)
  // Members as they will be once published, for the routing preview and the checks.
  const previewBackends = withPoolBreaker(backendById, memberIdList, breaker)
  const backendOrMissing = (backendId: string): BackendView | { id: string; missing: true } =>
    backendById.get(backendId.toLowerCase()) ?? { id: backendId, missing: true }
  // Only the pool's members are listed. Published members removed from the draft stay visible,
  // struck through, until Publish, so the removal can be undone.
  const rows = [
    ...draft.members.map((member) => ({ backend: backendOrMissing(member.backend_id), removed: false })),
    ...(creating ? [] : baselineRef.current.members)
      .filter((member) => !selectedIds.has(member.backend_id.toLowerCase()))
      .map((member) => ({ backend: backendOrMissing(member.backend_id), removed: true })),
  ]

  const issues = draftIssues(draft, mode)
  const validMembers = !issues.includes("members")
  const idValid = !creating || ID_PATTERN.test(newId.trim())
  const newApiPath = apiPath ?? suggestApiPath(draft.title, newId.trim(), capabilities.data?.id_prefix ?? "")
  const apiPathValid = !creating || !withApi
    || (API_PATH_PATTERN.test(newApiPath) && !apiPathConflict(newApiPath, apis.data ?? []))
  const dirty = creating ? draft.members.length > 0 || newId !== "" : signature(draft) !== baseline || breakerEdited
  const busy = save.isPending
  const hasEffectivePool = Boolean(pool.data)
  const primary = draft.members[0]?.backend_id
  const boundApis = creating ? [] : (apis.data ?? []).filter(
    (item) => item.binding?.pool_id.toLowerCase() === poolId.toLowerCase(),
  )
  const pendingIds = new Set(draft.members
    .map((member) => member.backend_id.toLowerCase())
    .filter((key) => creating || !effectiveIds.has(key)))
  // Only members publishing can't give the pool's rule: the ones this app didn't create.
  const missingBreakers = draft.members.filter((member) => {
    const backend = backendById.get(member.backend_id.toLowerCase())
    return !followsPool(backend) && !backend?.has_circuit_breaker
  })
  // The same-model check the server enforces on save. Sorted so reordering does not refetch.
  const modelCheck = useQuery({
    ...queries.modelCheck(draft.members.map((member) => member.backend_id).sort()),
    placeholderData: keepPreviousData,
  })
  const modelResult = modelCheck.isPlaceholderData ? undefined : modelCheck.data
  const modelOf = new Map(modelResult?.members.map((member) => [member.backend_id.toLowerCase(), member]))
  // The summary names the pool's model; a row repeats it only when its version differs.
  const versionsDiffer = new Set(modelResult?.members.flatMap((member) =>
    member.model_name ? [`${member.model_name} ${member.model_version ?? ""}`] : [])).size > 1

  const update = (next: Partial<PoolWrite>) => {
    setNotice(null)
    setDraft((current) => ({ ...current, ...next }))
  }
  const appendToDraft = (backendIds: string[]) => {
    const { members } = appendMembers(draft.members, backendIds)
    if (mode === null && members.length) setMode(deploymentMode(members))
    update({ members })
  }
  const dropMember = (backendId: string) => update({ members: removeMember(draft.members, backendId, mode) })
  // The pool's model as Azure reports it; the picker offers only deployments of that model.
  const poolModel = modelResult?.model_names.length === 1 ? modelResult.model_names[0]! : null
  // Accounts behind the Foundry members; the APIM identity needs a data-plane role on each.
  const memberAccounts = [...new Set((modelResult?.members ?? []).flatMap((member) =>
    member.deployment_id ? [member.deployment_id.split("/deployments/")[0]!] : []))]
  const changeMode = (next: Exclude<DeploymentMode, "custom">) => {
    setMode(next)
    update({ members: membersForMode(draft.members, next) })
  }

  if ((!creating && pool.isLoading) || backends.isLoading || capabilities.isLoading) {
    return <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
  }
  const loadError = pool.error ?? backends.error ?? capabilities.error
  if (loadError) return <ErrorText error={loadError} />

  const statusLabel = hasEffectivePool ? t.active : validMembers ? t.pending : t.disabledState
  const controls = <span className="editor-actions">
    {!creating && <span className="status-pill" data-active={hasEffectivePool && !dirty} data-pending={dirty}>{dirty ? t.pending : statusLabel}</span>}
    {!creating && <button type="button" className="icon-button" data-tour="glossary" title={t.glossaryTitle} aria-label={t.glossaryTitle} onClick={() => setGlossaryOpen(true)}><CircleHelp size={15} /></button>}
    {!readOnly && !creating && <button type="button" className="icon-button danger-hover" title={t.deletePool} aria-label={t.deletePool} disabled={busy} onClick={() => setDeleteOpen(true)}><Trash2 size={14} /></button>}
    {!readOnly && <Button variant="primary" disabled={busy || !dirty || !validMembers || issues.length > 0 || !idValid || !apiPathValid || modelResult?.ok === false} onClick={() => { save.reset(); setPublishOpen(true) }}>
      {save.isPending ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{save.isPending ? t.publishing : t.publish}
    </Button>}
  </span>

  const checks = checklistItems({ draft, backendById: previewBackends, boundApis, creating, dirty, modelCheck: modelResult }, t, locale)
  const firstUrl = boundApis[0] ? endpointUrl(capabilities.data?.gateway_url, boundApis[0].path) : null
  const summary = <div className="pool-summary" data-tour={creating ? undefined : "pool-summary"}>
    <div className="pool-summary-row">
      <span className="pool-facts">
        {mode && <span className="mode-chip">{t[mode]}</span>}
        <span>{fill(t.memberCount, { count: draft.members.length })}</span>
        {poolModel && <span className="model-tag">{poolModel}</span>}
      </span>
      <ChecklistBadge items={checks} open={checksOpen} onToggle={() => setChecksOpen(!checksOpen)} />
    </div>
    <div className="pool-endpoint">
      {firstUrl ? <>
        <Link2 size={14} />
        <code title={firstUrl}>{firstUrl}</code>
        <CopyButton text={firstUrl} label={t.endpoint} />
        {boundApis.length > 1 && <button type="button" className="link-button" onClick={() => setTab("connect")}>{fill(t.moreApis, { count: boundApis.length - 1 })}</button>}
      </> : <>
        <Link2Off size={14} />
        <span className="muted">{t.notBoundYet}</span>
        <button type="button" className="link-button" onClick={() => setTab("connect")}>{t.setUpApi}<ArrowRight size={13} /></button>
      </>}
    </div>
    {checksOpen && <PoolChecklist items={checks} />}
  </div>

  const tabs: Array<{ id: EditorTab; label: string; count?: number; warn?: boolean }> = [
    { id: "members", label: t.tabMembers, count: draft.members.length },
    { id: "routing", label: t.tabRouting },
    { id: "connect", label: t.tabConnect, count: boundApis.length },
    { id: "settings", label: t.tabSettings, warn: missingBreakers.length > 0 },
    // A pool that isn't published yet has no traffic.
    ...(creating ? [] : [{ id: "monitor" as const, label: t.tabMonitor }]),
  ]

  const membersPanel = <>
    <div className="mode-row" data-tour={creating ? undefined : "pool-mode"}>
      <span className="mode-label">{t.deploymentMode}<FieldHelp>{t.deploymentModeHint}</FieldHelp></span>
      <div className="segment" role="group" aria-label={t.deploymentMode}>
        {(["failover", "balanced", "weighted"] as const).map((candidate) =>
          <button key={candidate} type="button" className={validMembers && mode === candidate ? "active" : ""} aria-pressed={validMembers && mode === candidate} disabled={readOnly || busy || !validMembers} onClick={() => changeMode(candidate)}>
            <span className="label-full">{t[candidate]}</span><span className="label-short">{t[`${candidate}Short`]}</span>
          </button>)}
      </div>
    </div>
    {mode === "custom" && <div className="state warning"><CircleAlert size={14} />{t.customReadOnly}</div>}
    {mode === "failover" && validMembers && !readOnly && <div className="state">{t.failoverPrimaryHint}</div>}
    {issues.includes("weights") && <div className="state warning"><CircleAlert size={14} />{t.weightsMustDiffer}</div>}

    <div className="member-list" role="list" aria-label={t.members}>
      {!rows.length && <div className="empty"><b>{t.noMembersYet}</b></div>}
      {rows.map(({ backend, removed }) => {
        const key = backend.id.toLowerCase()
        const member = removed ? undefined : draft.members.find((candidate) => candidate.backend_id.toLowerCase() === key)
        const selected = Boolean(member)
        const isPrimary = member !== undefined && member.backend_id === primary
        const role = removed ? t.willRemove : isPrimary ? t.primary : mode === "failover" ? t.standby : t.member
        const missing = "missing" in backend
        const pending = !creating && !removed && !effectiveIds.has(key)
        const open = expanded === key && !missing
        const model = modelOf.get(key)
        return <div className="member" role="listitem" data-selected={selected} data-removed={removed} data-open={open} key={backend.id}>
          <div className="member-row">
            <span className="member-main">
              <b title={backend.id}>{missing ? backend.id : backend.title || backend.id}</b>
              <small>
                {missing ? <span className="danger">missing</span> : <>
                  {selected && model && (model.problem || !model.model_name || versionsDiffer) && <ModelTag model={model} />}
                  {!followsPool(backend) && !backend.has_circuit_breaker && <span className="warn">{t.noCircuitBreaker}</span>}
                  {pending && <span className="warn">{t.pending}</span>}
                </>}
              </small>
            </span>
            <div className="member-meta">
              {member && mode === "failover" && !readOnly
                ? <label className="member-role primary-choice" data-primary={isPrimary} data-selected={selected} title={isPrimary ? undefined : t.setPrimary}>
                  <input
                    type="radio"
                    name={primaryGroup}
                    checked={isPrimary}
                    disabled={busy}
                    aria-label={`${isPrimary ? t.primary : t.setPrimary}: ${missing ? backend.id : backend.title || backend.id}`}
                    onChange={() => update({ members: setPrimary(draft.members, backend.id, mode) })}
                  />
                  {role}
                </label>
                : <span className="member-role" data-primary={isPrimary} data-selected={selected}>{role}</span>}
              {member && mode === "weighted" && !readOnly
                ? <label className="member-weight"><small>{t.weight}</small><input type="number" min={1} max={100} value={member.weight} disabled={busy} aria-label={`${backend.id} ${t.weight}`} onChange={(event) => update({ members: changeWeight(draft.members, backend.id, Number(event.target.value)) })} /><em>{trafficShare(draft.members, backend.id)}%</em></label>
                : member && mode !== "failover" && <span className="member-share">{trafficShare(draft.members, backend.id)}%</span>}
              {removed && !readOnly && <Button variant="ghost" disabled={busy || draft.members.length >= MAX_MEMBERS} onClick={() => appendToDraft([backend.id])}><RotateCcw size={13} />{t.undo}</Button>}
              {/* Failover picks the primary with the role's radio instead. */}
              {selected && !isPrimary && mode !== "custom" && mode !== "failover" && !readOnly && <button type="button" className="icon-button" title={t.setPrimary} aria-label={t.setPrimary} disabled={busy} onClick={() => update({ members: setPrimary(draft.members, backend.id, mode) })}><Star size={13} /></button>}
              {member && !readOnly && <button type="button" className="icon-button" title={t.removeFromPool} aria-label={`${t.removeFromPool}: ${backend.id}`} disabled={busy} onClick={() => dropMember(backend.id)}><X size={14} /></button>}
              {/* Foundry members are fixed by their deployment; custom endpoints can be edited. */}
              {!missing && !readOnly && followsPool(backend) && model && !model.deployment_id && <button type="button" className="icon-button" title={t.editEndpoint} aria-label={`${t.editEndpoint}: ${backend.id}`} disabled={busy} onClick={() => setEditingMember(backend)}><Pencil size={13} /></button>}
              {!missing && <button type="button" className="icon-button" title={t.showDetails} aria-label={`${t.showDetails}: ${backend.id}`} aria-expanded={open} onClick={() => setExpanded(open ? null : key)}><ChevronDown size={14} className="chevron" /></button>}
            </div>
          </div>
          {open && !missing && <dl className="member-details">
            <div className="wide"><dt>{t.url}</dt><dd><code>{backend.url}</code></dd></div>
            <div><dt>{t.auth}</dt><dd>{authLabel(backend.auth_type, t)}</dd></div>
            <div><dt>{t.circuitBreaker}</dt><dd>{breakerLabel(previewBackends.get(key) ?? backend, t) ?? <span className="warn">—</span>}</dd></div>
            {member && <div><dt>{t.priority}</dt><dd>{`P${member.priority + 1}`}</dd></div>}
            {member && <div><dt>{t.weight}</dt><dd>{member.weight}</dd></div>}
            {model?.model_name && <div><dt>{t.model}</dt><dd title={model.deployment_id ?? ""}>{model.model_name}{model.model_version && ` ${model.model_version}`}</dd></div>}
            <div><dt>{t.ownership}</dt><dd>{backend.protected ? t.protected : backend.managed ? t.managed : t.external}</dd></div>
            {!creating && !removed && <div><dt>{t.status}</dt><dd>{pending ? t.pending : t.active}</dd></div>}
          </dl>}
        </div>
      })}
    </div>
    {!readOnly && <div className="member-actions">
      <Button disabled={busy || draft.members.length >= MAX_MEMBERS} onClick={() => setPickerOpen(true)}>
        <ListPlus size={13} />{poolModel ? fill(t.addDeployments, { model: poolModel }) : t.addMembersTitle}
      </Button>
      {draft.members.length >= MAX_MEMBERS && <small className="muted">{t.poolFull}</small>}
    </div>}
    {editingMember && <MemberBackendDialog backend={editingMember} breaker={breaker} onClose={() => setEditingMember(null)} />}
    {pickerOpen && <AddMembersDialog
      model={poolModel}
      breaker={breaker}
      memberIds={selectedIds}
      room={MAX_MEMBERS - draft.members.length}
      onAdd={(backendIds) => { appendToDraft(backendIds); setPickerOpen(false) }}
      onClose={() => setPickerOpen(false)}
    />}
    {!validMembers && !readOnly && <div className="state warning"><CircleAlert size={14} />{t.needMembers}</div>}
    <ModelCheckNotice check={modelResult} loading={draft.members.length > 0 && modelCheck.isFetching && !modelResult} />
  </>

  const affinityBlock = <div className="affinity">
    <div className="affinity-control">
      <label><input type="checkbox" checked={draft.session_affinity} disabled={readOnly || busy || !affinitySupported || !validMembers} onChange={(event) => update({ session_affinity: event.target.checked })} /><span>{t.sessionAffinity}</span></label>
      <FieldHelp>{t.sessionAffinityHelp}</FieldHelp>
      <span className="affinity-state">{pool.data?.session_affinity ? t.affinityOn : draft.session_affinity ? t.affinityPending : t.affinityOff}</span>
    </div>
    {!affinitySupported && <div className="state warning"><CircleAlert size={14} />{t.affinityUnsupported}</div>}
    {draft.session_affinity && <dl>
      <div><dt>{t.scope}</dt><dd>{t.scopeValue}</dd></div>
      <div><dt>{t.cookie}</dt><dd><code>{pool.data?.session_cookie_name ?? "PoolAffinity-…"}</code></dd></div>
      <div><dt>{t.failureBehavior}</dt><dd>{t.failureBehaviorValue}</dd></div>
    </dl>}
  </div>

  const breakerBlock = <>
    <PoolBreakerSettings
      rule={breaker}
      uniform={derivedBreaker.uniform}
      edited={breakerEdited}
      disabled={readOnly || busy}
      shared={breakerShared}
      readOnlyIds={readOnlyMembers(memberIdList, backendById)}
      onChange={setBreakerEdit}
      onReset={() => setBreakerEdit(null)}
    />
    {missingBreakers.length > 0 && <div className="state warning"><CircleAlert size={14} />{t.noCircuitBreaker}: {missingBreakers.map((member) => member.backend_id).join(", ")}</div>}
  </>

  const settingsPanel = <>
    {affinityBlock}
    {breakerBlock}
  </>

  const editor = <section className="resilience">
    {creating && <div className="form-grid">
      <Field label={t.poolId} hint={t.idHint}>
        <input value={newId} onChange={(event) => setNewId(event.target.value)} placeholder={`${capabilities.data?.id_prefix ?? ""}gpt-4o`} aria-invalid={newId !== "" && !idValid} autoFocus />
      </Field>
      <Field label={t.title}>
        <input value={draft.title ?? ""} onChange={(event) => update({ title: event.target.value || null })} />
      </Field>
      <label className="check span-2"><input type="checkbox" checked={withApi} onChange={(event) => setWithApi(event.target.checked)} />{t.createApiWithPool}</label>
      {withApi && <div className="span-2">
        <ApiPathField path={newApiPath} apis={apis.data ?? []} gatewayUrl={capabilities.data?.gateway_url} onChange={setApiPath} />
      </div>}
    </div>}
    {!creating && summary}
    {pool.data?.protected && <div className="state warning"><ShieldCheck size={14} />{t.protectedPool}</div>}
    {readOnly && !pool.data?.protected && <div className="state"><ShieldCheck size={14} />{t.readOnly}</div>}
    {addedCount !== null && (addedCount > 0
      ? dirty && <div className="state warning" role="status"><CircleAlert size={14} />{fill(t.addedFromDeployments, { count: addedCount })}</div>
      : <div className="state" role="status"><CheckCircle2 size={14} />{t.alreadyInPool}</div>)}
    {notice && !dirty && <div className="state" data-status="active"><CheckCircle2 size={14} />{notice}</div>}
    <ErrorText error={save.error} />

    {creating ? <>
      {membersPanel}
      {affinityBlock}
      {breakerBlock}
    </> : <>
      <div className="subtabs" role="tablist" aria-label={pool.data?.title || poolId}>
        {tabs.map((item) => <button key={item.id} type="button" role="tab" id={`${tabIdPrefix}-${item.id}`} aria-selected={tab === item.id} aria-controls={`${tabIdPrefix}-${item.id}-panel`} className={tab === item.id ? "active" : ""} data-tour={`pool-tab-${item.id}`} onClick={() => setTab(item.id)}>
          {item.label}
          {item.count !== undefined && <span className="subtab-count">{item.count}</span>}
          {item.warn && <span className="subtab-warn" aria-hidden="true" />}
        </button>)}
      </div>
      {/* Panels stay mounted so their state (and the post-create API) survives switching tabs. */}
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-members-panel`} aria-labelledby={`${tabIdPrefix}-members`} hidden={tab !== "members"}>{membersPanel}</div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-routing-panel`} aria-labelledby={`${tabIdPrefix}-routing`} hidden={tab !== "routing"}>
        <RoutingPreview
          draft={draft}
          backendById={previewBackends}
          boundApis={boundApis}
          apimName={capabilities.data?.apim_name}
          dirty={dirty}
          pendingIds={pendingIds}
        />
      </div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-connect-panel`} aria-labelledby={`${tabIdPrefix}-connect`} hidden={tab !== "connect"}>
        {pool.data && <ApiBindings pool={pool.data} canWrite={me.data?.can_write === true} autoCreate={autoCreateApi} onAutoCreateStarted={onAutoCreateApiStarted} />}
        {pool.data && <CallGuide
          apis={boundApis}
          capabilities={capabilities.data}
          model={poolModel}
          sessionAffinity={pool.data.session_affinity}
          accountIds={memberAccounts}
        />}
      </div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-settings-panel`} aria-labelledby={`${tabIdPrefix}-settings`} hidden={tab !== "settings"}>{settingsPanel}</div>
      {!creating && pool.data && <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-monitor-panel`} aria-labelledby={`${tabIdPrefix}-monitor`} hidden={tab !== "monitor"}>
        <PoolMonitor poolId={pool.data.id} active={tab === "monitor"} apimResourceId={capabilities.data?.apim_resource_id} />
      </div>}
    </>}
    {!header && <footer className="editor-footer">{controls}</footer>}
    <GlossaryDialog open={glossaryOpen} onClose={() => setGlossaryOpen(false)} />

    <PublishDialog
      open={publishOpen}
      changes={[
        ...poolChanges(creating ? null : baselineRef.current, draft),
        ...breakerUpdates.length ? [{ kind: "breaker" as const, count: breakerUpdates.length }] : [],
      ]}
      createApiUrl={creating && withApi ? endpointUrl(capabilities.data?.gateway_url, newApiPath) : undefined}
      boundApis={boundApis}
      busy={save.isPending}
      error={save.error}
      onConfirm={() => save.mutate()}
      onClose={() => setPublishOpen(false)}
    />
    {/* Mounted only while open, so it reads what removing takes with it fresh each time. */}
    {deleteOpen && poolId && <RemovePoolDialog
      poolId={poolId}
      etag={pool.data?.etag}
      onClose={() => setDeleteOpen(false)}
      onRemoved={() => { setDeleteOpen(false); onDeleted?.() }}
    />}
  </section>
  return header ? <>{header(controls)}{editor}</> : editor
}

function ModelTag({ model }: { model: MemberModel | undefined }) {
  const { t } = useI18n()
  if (!model) return null
  if (model.problem) return <span className="danger" title={model.problem}>{t.modelUnknown}</span>
  if (!model.model_name) return <span title={t.notFoundryDeployment}>{t.notFoundryDeployment}</span>
  return <span className="model-tag" title={model.deployment_id ?? ""}>
    {model.model_name}{model.model_version && ` ${model.model_version}`}
  </span>
}
