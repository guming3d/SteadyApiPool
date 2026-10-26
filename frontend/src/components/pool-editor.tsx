// The pool editor. Members are rows in the LiteLLM gateway's model table, all under the pool's
// name, and saving writes them to the gateway directly.
import { CheckCircle2, ChevronDown, CircleAlert, CircleHelp, Link2, ListPlus, LoaderCircle, Pencil, RotateCcw, Send, ShieldCheck, Star, Trash2, X } from "lucide-react"
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import { draftSettings, poolSettings, sameSettings, type PoolSettings } from "../pool-breaker"
import {
  addMember,
  appendMembers,
  changeWeight,
  deploymentMode,
  draftIssues,
  MAX_MEMBERS,
  membersForMode,
  POOL_ID_PATTERN,
  primaryFirst,
  removeMember,
  setPrimary,
  trafficShare,
  type DeploymentMode,
} from "../pool-modes"
import { poolChanges } from "../pool-changes"
import type { BackendView, MemberModel, PoolView, PoolWrite } from "../types"
import { authLabel, targetLabel } from "./backend-labels"
import { CallGuide, CopyButton } from "./call-guide"
import { MemberBackendDialog } from "./member-backend-dialog"
import { AddMembersDialog } from "./member-picker"
import { PoolBreakerSettings } from "./pool-breaker-settings"
import { baseUrlOf, PoolEndpointPanel } from "./pool-endpoint"
import { ChecklistBadge, checklistItems, GlossaryDialog, ModelCheckNotice, PoolChecklist, PublishDialog } from "./pool-guide"
import { PoolMonitor } from "./pool-monitor"
import { RemovePoolDialog } from "./removal-dialog"
import { RoutingPreview } from "./routing-preview"
import { Button, ErrorText, Field, FieldHelp } from "./ui"

function draftFromPool(pool: PoolView | null): PoolWrite {
  if (!pool) return { members: [], title: null, description: null, ...poolSettings(null) }
  return {
    members: primaryFirst(pool.members),
    title: pool.title,
    description: null,
    ...poolSettings(pool),
  }
}

const signature = (draft: PoolWrite) => JSON.stringify(draft)

export type EditorTab = "members" | "routing" | "connect" | "settings" | "monitor"

export type PoolCreateRequest = { id: string; title: string; members: string[] }
/** Opens an existing pool with these members added to the draft, e.g. imported deployments. */
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
  openOn,
  onCreated,
  onDeleted,
  header,
}: {
  /** Omit to create a new pool. */
  poolId?: string
  /** Prefills a new pool, e.g. from imported deployments. */
  initial?: PoolCreateRequest
  /** Members to add to an existing pool's draft once it loads; publishing stays manual. */
  addMembers?: string[]
  /** The tab to open on, e.g. Connect right after the pool was created. */
  openOn?: EditorTab
  onCreated?: (poolId: string) => void
  onDeleted?: () => void
  header?: (controls: ReactNode) => ReactNode
}) {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const creating = poolId === undefined
  const me = useQuery(queries.me())
  const capabilities = useQuery(queries.capabilities())
  const backends = useQuery(queries.backends())
  const pools = useQuery(queries.pools())
  const pool = useQuery({ ...queries.pool(poolId ?? ""), enabled: !creating })
  const endpoint = useQuery({ ...queries.endpoint(poolId ?? ""), enabled: !creating })
  const readOnly = !me.data?.can_write || pool.data?.protected === true || (pool.data !== undefined && !pool.data.managed)

  const baselineRef = useRef(draftFromPool(null))
  const [baseline, setBaseline] = useState(() => signature(baselineRef.current))
  const [draft, setDraft] = useState<PoolWrite>(() => initialDraft(initial))
  const [mode, setMode] = useState<DeploymentMode | null>(initial?.members.length ? "failover" : null)
  const [newId, setNewId] = useState(initial?.id ?? "")
  const [notice, setNotice] = useState<string | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [publishOpen, setPublishOpen] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [tab, setTab] = useState<EditorTab>(openOn ?? "members")
  const tabIdPrefix = `pool-tab${useId().replace(/[^A-Za-z0-9]/g, "")}`
  // In failover the role is a radio: exactly one member is primary.
  const primaryGroup = useId()
  const [checksOpen, setChecksOpen] = useState(false)
  const [glossaryOpen, setGlossaryOpen] = useState(false)
  // Lower-case ID of the member whose details are shown.
  const [expanded, setExpanded] = useState<string | null>(null)
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
      return api.savePool(id, { ...draft, etag: creating ? null : pool.data?.etag ?? null })
    },
    onSuccess: (saved) => {
      // The saved pool becomes both baseline and draft, so the editor is clean afterwards.
      const next = draftFromPool(saved)
      baselineRef.current = next
      setBaseline(signature(next))
      setDraft(next)
      setMode(deploymentMode(next.members))
      queryClient.setQueryData(keys.pool(saved.id), saved)
      void queryClient.invalidateQueries({ queryKey: keys.pools, exact: true })
      void queryClient.invalidateQueries({ queryKey: keys.poolConfig(saved.id) })
      // Members now list this pool, and removed custom endpoints no pool uses are gone.
      void queryClient.invalidateQueries({ queryKey: keys.backends })
      void queryClient.invalidateQueries({ queryKey: keys.deployments })
      setNotice(t.saved)
      setPublishOpen(false)
      if (creating) onCreated?.(saved.id)
    },
  })

  const backendById = useMemo(
    () => new Map((backends.data ?? []).map((backend) => [backend.id.toLowerCase(), backend])),
    [backends.data],
  )
  const selectedIds = new Set(draft.members.map((member) => member.backend_id.toLowerCase()))
  const effectiveIds = new Set(pool.data?.members.map((member) => member.backend_id.toLowerCase()))
  const settings = draftSettings(draft)
  const settingsEdited = !creating && !sameSettings(settings, draftSettings(baselineRef.current))
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
  const trimmedId = newId.trim()
  const idTaken = creating && (pools.data ?? []).some((item) => item.id.toLowerCase() === trimmedId.toLowerCase())
  const idValid = !creating || (POOL_ID_PATTERN.test(trimmedId) && !idTaken)
  const dirty = creating ? draft.members.length > 0 || newId !== "" : signature(draft) !== baseline
  const busy = save.isPending
  const hasEffectivePool = Boolean(pool.data)
  const primary = draft.members[0]?.backend_id
  const pendingIds = new Set(draft.members
    .map((member) => member.backend_id.toLowerCase())
    .filter((key) => creating || !effectiveIds.has(key)))
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
  const updateSettings = (next: PoolSettings) => update(next)
  const appendToDraft = (backendIds: string[]) => {
    const { members } = appendMembers(draft.members, backendIds)
    if (mode === null && members.length) setMode(deploymentMode(members))
    update({ members })
  }
  const dropMember = (backendId: string) => update({ members: removeMember(draft.members, backendId, mode) })
  // The pool's model as Azure reports it; the picker offers only deployments of that model.
  const poolModel = modelResult?.model_names.length === 1 ? modelResult.model_names[0]! : null
  // Accounts behind the Foundry members; the gateway's identity needs a data-plane role on each.
  const memberAccounts = [...new Set((modelResult?.members ?? []).flatMap((member) =>
    member.deployment_id ? [member.deployment_id.split("/deployments/")[0]!] : []))]
  const usesIdentity = draft.members.some((member) =>
    backendById.get(member.backend_id.toLowerCase())?.auth_type === "managed_identity")
  const changeMode = (next: Exclude<DeploymentMode, "custom">) => {
    setMode(next)
    update({ members: membersForMode(draft.members, next) })
  }

  if ((!creating && pool.isLoading) || backends.isLoading || capabilities.isLoading) {
    return <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
  }
  const loadError = pool.error ?? backends.error ?? capabilities.error
  if (loadError) return <ErrorText error={loadError} />

  const modelName = creating ? trimmedId || "<pool>" : poolId
  const baseUrl = baseUrlOf(endpoint.data, capabilities.data?.gateway_url)
  const statusLabel = hasEffectivePool ? t.active : validMembers ? t.pending : t.disabledState
  const controls = <span className="editor-actions">
    {!creating && <span className="status-pill" data-active={hasEffectivePool && !dirty} data-pending={dirty}>{dirty ? t.pending : statusLabel}</span>}
    {!creating && <button type="button" className="icon-button" data-tour="glossary" title={t.glossaryTitle} aria-label={t.glossaryTitle} onClick={() => setGlossaryOpen(true)}><CircleHelp size={15} /></button>}
    {!readOnly && !creating && <button type="button" className="icon-button danger-hover" title={t.deletePool} aria-label={t.deletePool} disabled={busy} onClick={() => setDeleteOpen(true)}><Trash2 size={14} /></button>}
    {!readOnly && <Button variant="primary" disabled={busy || !dirty || !validMembers || issues.length > 0 || !idValid || modelResult?.ok === false} onClick={() => { save.reset(); setPublishOpen(true) }}>
      {save.isPending ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{save.isPending ? t.publishing : t.publish}
    </Button>}
  </span>

  const checks = checklistItems({
    draft,
    backendById,
    creating,
    dirty,
    modelCheck: modelResult,
    identityReady: Boolean(capabilities.data?.gateway_principal_id),
  }, t, locale)
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
      <Link2 size={14} />
      <code title={baseUrl}>{baseUrl}</code>
      <span className="muted">·</span>
      <code title={t.modelName}>{modelName}</code>
      <CopyButton text={modelName} label={t.modelName} />
      {(endpoint.data?.aliases.length ?? 0) > 0 && <button type="button" className="link-button" onClick={() => setTab("connect")}>{fill(t.moreAliases, { count: endpoint.data!.aliases.length })}</button>}
    </div>
    {checksOpen && <PoolChecklist items={checks} />}
  </div>

  const tabs: Array<{ id: EditorTab; label: string; count?: number }> = [
    { id: "members", label: t.tabMembers, count: draft.members.length },
    { id: "routing", label: t.tabRouting },
    { id: "connect", label: t.tabConnect },
    { id: "settings", label: t.tabSettings },
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
        const name = missing ? backend.id : backend.title || backend.id
        return <div className="member" role="listitem" data-selected={selected} data-removed={removed} data-open={open} key={backend.id}>
          <div className="member-row">
            <span className="member-main">
              <b title={backend.id}>{name}</b>
              <small>
                {missing ? <span className="danger">{t.statusMissing}</span> : <>
                  {selected && model && (model.problem || !model.model_name || versionsDiffer) && <ModelTag model={model} />}
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
                    aria-label={`${isPrimary ? t.primary : t.setPrimary}: ${name}`}
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
              {!missing && !readOnly && backend.managed && model && !model.deployment_id && <button type="button" className="icon-button" title={t.editEndpoint} aria-label={`${t.editEndpoint}: ${backend.id}`} disabled={busy} onClick={() => setEditingMember(backend)}><Pencil size={13} /></button>}
              {!missing && <button type="button" className="icon-button" title={t.showDetails} aria-label={`${t.showDetails}: ${backend.id}`} aria-expanded={open} onClick={() => setExpanded(open ? null : key)}><ChevronDown size={14} className="chevron" /></button>}
            </div>
          </div>
          {open && !missing && <dl className="member-details">
            <div className="wide"><dt>{t.url}</dt><dd><code>{backend.url}</code></dd></div>
            <div><dt>{t.litellmModel}</dt><dd><code>{targetLabel(backend)}</code></dd></div>
            <div><dt>{t.auth}</dt><dd>{authLabel(backend.auth_type, t)}</dd></div>
            {member && <div><dt>{t.priority}</dt><dd>{`P${member.priority + 1}`}</dd></div>}
            {member && <div><dt>{t.weight}</dt><dd>{member.weight}</dd></div>}
            {model?.model_name && <div><dt>{t.model}</dt><dd title={model.deployment_id ?? ""}>{model.model_name}{model.model_version && ` ${model.model_version}`}</dd></div>}
            <div><dt>{t.ownership}</dt><dd>{backend.protected ? t.protected : backend.managed ? t.managed : t.external}</dd></div>
            {backend.pools.length > 1 && <div className="wide"><dt>{t.alsoIn}</dt><dd>{backend.pools.filter((id) => id.toLowerCase() !== (poolId ?? "").toLowerCase()).join(", ")}</dd></div>}
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
    {editingMember && <MemberBackendDialog backend={editingMember} onClose={() => setEditingMember(null)} />}
    {pickerOpen && <AddMembersDialog
      model={poolModel}
      memberIds={selectedIds}
      room={MAX_MEMBERS - draft.members.length}
      onAdd={(backendIds) => { appendToDraft(backendIds); setPickerOpen(false) }}
      onClose={() => setPickerOpen(false)}
    />}
    {!validMembers && !readOnly && <div className="state warning"><CircleAlert size={14} />{t.needMembers}</div>}
    <ModelCheckNotice check={modelResult} loading={draft.members.length > 0 && modelCheck.isFetching && !modelResult} />
  </>

  const settingsPanel = <PoolBreakerSettings
    settings={settings}
    edited={settingsEdited}
    disabled={readOnly || busy}
    onChange={updateSettings}
    onReset={() => updateSettings(draftSettings(baselineRef.current))}
  />

  const editor = <section className="resilience">
    {creating && <div className="form-grid">
      <Field label={t.poolId} hint={idTaken ? t.poolIdTaken : t.poolIdHint}>
        <input value={newId} onChange={(event) => setNewId(event.target.value)} placeholder={poolModel ?? "gpt-4o"} aria-invalid={newId !== "" && !idValid} autoFocus />
      </Field>
      <Field label={t.title}>
        <input value={draft.title ?? ""} onChange={(event) => update({ title: event.target.value || null })} />
      </Field>
      <div className="state span-2"><Link2 size={14} />{fill(t.newPoolEndpoint, { url: baseUrl, model: modelName })}</div>
    </div>}
    {!creating && summary}
    {pool.data?.protected && <div className="state warning"><ShieldCheck size={14} />{t.protectedPool}</div>}
    {pool.data && !pool.data.managed && !pool.data.protected && <div className="state"><ShieldCheck size={14} />{t.foreignPool}</div>}
    {readOnly && pool.data?.managed && !pool.data.protected && <div className="state"><ShieldCheck size={14} />{t.readOnly}</div>}
    {addedCount !== null && (addedCount > 0
      ? dirty && <div className="state warning" role="status"><CircleAlert size={14} />{fill(t.addedFromDeployments, { count: addedCount })}</div>
      : <div className="state" role="status"><CheckCircle2 size={14} />{t.alreadyInPool}</div>)}
    {notice && !dirty && <div className="state" data-status="active"><CheckCircle2 size={14} />{notice}</div>}
    <ErrorText error={save.error} />

    {creating ? <>
      {membersPanel}
      {settingsPanel}
    </> : <>
      <div className="subtabs" role="tablist" aria-label={pool.data?.title || poolId}>
        {tabs.map((item) => <button key={item.id} type="button" role="tab" id={`${tabIdPrefix}-${item.id}`} aria-selected={tab === item.id} aria-controls={`${tabIdPrefix}-${item.id}-panel`} className={tab === item.id ? "active" : ""} data-tour={`pool-tab-${item.id}`} onClick={() => setTab(item.id)}>
          {item.label}
          {item.count !== undefined && <span className="subtab-count">{item.count}</span>}
        </button>)}
      </div>
      {/* Panels stay mounted so their state survives switching tabs. */}
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-members-panel`} aria-labelledby={`${tabIdPrefix}-members`} hidden={tab !== "members"}>{membersPanel}</div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-routing-panel`} aria-labelledby={`${tabIdPrefix}-routing`} hidden={tab !== "routing"}>
        <RoutingPreview
          draft={draft}
          backendById={backendById}
          modelName={modelName}
          gatewayName={capabilities.data?.gateway_name}
          dirty={dirty}
          pendingIds={pendingIds}
        />
      </div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-connect-panel`} aria-labelledby={`${tabIdPrefix}-connect`} hidden={tab !== "connect"}>
        {pool.data && <PoolEndpointPanel pool={pool.data} canWrite={me.data?.can_write === true} />}
        {pool.data && <CallGuide
          endpoint={endpoint.data}
          capabilities={capabilities.data}
          canWrite={me.data?.can_write === true}
          usesIdentity={usesIdentity}
          accountIds={memberAccounts}
        />}
      </div>
      <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-settings-panel`} aria-labelledby={`${tabIdPrefix}-settings`} hidden={tab !== "settings"}>{settingsPanel}</div>
      {!creating && pool.data && <div className="tab-panel" role="tabpanel" id={`${tabIdPrefix}-monitor-panel`} aria-labelledby={`${tabIdPrefix}-monitor`} hidden={tab !== "monitor"}>
        <PoolMonitor poolId={pool.data.id} active={tab === "monitor"} capabilities={capabilities.data} />
      </div>}
    </>}
    {!header && <footer className="editor-footer">{controls}</footer>}
    <GlossaryDialog open={glossaryOpen} onClose={() => setGlossaryOpen(false)} />

    <PublishDialog
      open={publishOpen}
      changes={poolChanges(creating ? null : baselineRef.current, draft)}
      endpoint={{ baseUrl, model: modelName }}
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
