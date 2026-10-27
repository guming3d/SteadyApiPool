import { CheckCircle2, CircleAlert, ListPlus, LoaderCircle, Network, RefreshCw, Rocket, Search } from "lucide-react"
import { useCallback, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import { deploymentMode, MAX_MEMBERS, primaryFirst } from "../pool-modes"
import type { DeploymentView, FoundryDeploymentStatus } from "../types"
import { DeployModelDialog } from "./deploy-model-dialog"
import { useProductUi, type PoolAddRequest, type PoolCreateRequest } from "../product"
import { FoundryAccessNotice, useOpenSetup } from "./setup"
import { Button, Dialog, ErrorText } from "./ui"

export const refKey = (view: DeploymentView) => `${view.account_id.toLowerCase()}|${view.deployment}`
const modelKey = (view: DeploymentView) => `${view.model_name}@${view.model_version}`
// Pools must serve one model (the server enforces it); versions may differ.
export const modelName = (view: DeploymentView) => view.model_name.toLowerCase()

function poolIdFor(prefix: string, model: string) {
  return `${prefix}${model}`.replace(/[^A-Za-z0-9-]+/g, "-").replace(/-{2,}/g, "-").replace(/^-|-$/g, "").slice(0, 80)
}

export function DeploymentsPage({
  onCreatePool,
  onAddToPool,
}: {
  onCreatePool: (request: PoolCreateRequest) => void
  onAddToPool: (request: PoolAddRequest) => void
}) {
  const { product, useFacts, membersKey } = useProductUi()
  const { t } = useI18n(product)
  const queryClient = useQueryClient()
  const me = useQuery(queries.me())
  const facts = useFacts()
  const deployments = useQuery(queries.deployments())
  const [filter, setFilter] = useState("")
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [notice, setNotice] = useState<string | null>(null)
  const [addOpen, setAddOpen] = useState(false)
  const [deployOpen, setDeployOpen] = useState(false)
  const canWrite = me.data?.can_write === true
  const openSetup = useOpenSetup()

  const refresh = useMutation({
    mutationFn: () => api.deployments(true),
    onSuccess: (data) => queryClient.setQueryData(keys.deployments, data),
  })
  // Names already used in each account, so the dialog suggests a free one.
  const existing = useMemo(() => {
    const byAccount = new Map<string, string[]>()
    for (const view of deployments.data ?? []) {
      const key = view.account_id.toLowerCase()
      byAccount.set(key, [...byAccount.get(key) ?? [], view.deployment])
    }
    return byAccount
  }, [deployments.data])
  const deployed = useCallback((status: FoundryDeploymentStatus) => {
    setDeployOpen(false)
    setNotice(fill(t.deployedNotice, { name: status.deployment }))
    refresh.mutate()
  }, [refresh, t])
  const groups = useMemo(() => {
    const normalized = filter.trim().toLowerCase()
    const byModel = new Map<string, DeploymentView[]>()
    for (const view of deployments.data ?? []) {
      const haystack = `${view.model_name} ${view.deployment} ${view.account_name} ${view.location}`.toLowerCase()
      if (normalized && !haystack.includes(normalized)) continue
      byModel.set(modelKey(view), [...byModel.get(modelKey(view)) ?? [], view])
    }
    // Models with more deployments first: those are the ones a pool can balance across.
    return [...byModel.entries()].sort((left, right) =>
      right[1].length - left[1].length || left[0].localeCompare(right[0]))
  }, [deployments.data, filter])

  const all = deployments.data ?? []
  const selectedViews = all.filter((view) => selected.has(refKey(view)))
  const selectedModels = new Set(selectedViews.map(modelName))
  const selectedVersions = new Set(selectedViews.map(modelKey))
  const selectedAccounts = [...new Set(selectedViews.map((view) => view.account_id))]
  const principalId = facts?.principalId ?? null

  const importViews = useMutation({
    mutationFn: async ({ views }: { views: DeploymentView[]; createPool?: boolean; addToPool?: string }) => {
      const result = await api.importDeployments(views.map(({ account_id, deployment }) => ({ account_id, deployment })))
      // The pool editor validates members against this list, so wait for it.
      await queryClient.refetchQueries({ queryKey: membersKey })
      await queryClient.invalidateQueries({ queryKey: keys.deployments })
      return result
    },
    onSuccess: (result, { views, createPool, addToPool }) => {
      setSelected(new Set())
      setAddOpen(false)
      if (addToPool) {
        onAddToPool({ poolId: addToPool, members: result.backend_ids })
        return
      }
      setNotice(`${t.importedNotice}: ${result.created.length} / ${result.backend_ids.length}`)
      if (createPool) {
        const model = views[0]!.model_name
        onCreatePool({ id: poolIdFor(facts?.idPrefix ?? "", model), title: model, members: result.backend_ids })
      }
    },
  })
  const toggle = (view: DeploymentView, checked: boolean) => {
    setNotice(null)
    setSelected((current) => {
      const next = new Set(current)
      if (checked) next.add(refKey(view))
      else next.delete(refKey(view))
      return next
    })
  }
  const toggleGroup = (views: DeploymentView[], checked: boolean) => {
    setNotice(null)
    setSelected((current) => {
      const next = new Set(current)
      for (const view of views) {
        if (checked) next.add(refKey(view))
        else next.delete(refKey(view))
      }
      return next
    })
  }
  const busy = importViews.isPending

  return <div className="page">
    <header className="page-header">
      <div><h1>{t.deployments}</h1><span>{deployments.data?.length ?? "—"}</span></div>
      <span className="editor-actions">
        <Button onClick={() => refresh.mutate()} disabled={refresh.isPending || deployments.isFetching}>
          <RefreshCw size={14} className={refresh.isPending ? "spin" : ""} />{t.refresh}
        </Button>
        {/* Azure decides with the user's own rights; signing in is all the app asks. */}
        {me.data && !me.data.guest && <Button variant="primary" onClick={() => setDeployOpen(true)}><Rocket size={14} />{t.deployModel}</Button>}
      </span>
    </header>
    {deployOpen && <DeployModelDialog existing={existing} onDeployed={deployed} onClose={() => setDeployOpen(false)} />}
    <p className="muted page-hint">{t.deploymentsHint}{facts && <> · {t.subscriptions}: <code>{facts.discoverySubscriptions.join(", ")}</code></>}</p>

    {facts && !principalId && <div className="callout warning">
      <CircleAlert size={15} /><div><b>{t.identityMissing}</b><span>{t.identityMissingFix}</span></div>
      <Button onClick={openSetup}>{t.setupOpen}</Button>
    </div>}

    <label className="search deployments-search"><Search size={14} /><input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={t.filterModels} aria-label={t.filterModels} /></label>

    <div data-tour="deployments-list">
    {deployments.isLoading || refresh.isPending ? <div className="state"><LoaderCircle className="spin" size={14} />{t.scanning}</div>
      : deployments.error ? <ErrorText error={deployments.error} />
      : !groups.length ? <div className="empty"><Network size={28} /><b>{filter ? t.noMatch : t.noDeployments}</b><span>{t.noDeploymentsHint}</span></div>
      : <div className="model-groups">{groups.map(([key, views]) => {
        const groupSelected = views.every((view) => selected.has(refKey(view)))
        const regions = new Set(views.map((view) => view.location))
        return <section className="card model-group" key={key}>
          <header>
            <label className="check">
              <input type="checkbox" checked={groupSelected} disabled={!canWrite || busy} aria-label={`${t.selectAll} ${views[0]!.model_name}`} onChange={(event) => toggleGroup(views, event.target.checked)} />
              <b>{views[0]!.model_name}</b>
            </label>
            <code className="muted">{views[0]!.model_version}</code>
            <span className="muted">{views.length} {t.deploymentCount} · {regions.size} {t.regions}</span>
            {canWrite && views.length >= 2 && <Button className="group-action" disabled={busy} onClick={() => importViews.mutate({ views, createPool: true })}>
              <Network size={13} />{t.createPoolFromModel}
            </Button>}
          </header>
          <table className="table">
            <tbody>{views.map((view) => <tr key={refKey(view)} data-selected={selected.has(refKey(view))}>
              <td className="check-cell"><input type="checkbox" checked={selected.has(refKey(view))} disabled={!canWrite || busy} aria-label={`${view.account_name} ${view.deployment}`} onChange={(event) => toggle(view, event.target.checked)} /></td>
              <td><b>{view.deployment}</b> <code className="muted" title={view.backend_url}>{view.account_name}</code></td>
              <td title={view.resource_group}>{view.location}</td>
              <td title={view.local_auth_disabled ? t.keyAuthDisabled : undefined}>{view.sku}{view.capacity !== null && <small className="muted" title="SKU capacity (K TPM for Standard SKUs, PTU for provisioned)"> · {view.capacity}</small>}</td>
              <td className="actions">{view.backend_id
                ? <span className="imported" title={view.backend_id}><CheckCircle2 size={13} />{t.imported}</span>
                : <span className="muted">{t.notImported}</span>}</td>
            </tr>)}</tbody>
          </table>
        </section>
      })}</div>}
    </div>

    {notice && <div className="state" data-status="active"><CheckCircle2 size={14} />{notice}</div>}
    <ErrorText error={importViews.error ?? refresh.error} />

    {selectedViews.length > 0 && <div className="selection-bar">
      <div className="selection-summary">
        <b>{selectedViews.length} {t.selected}</b>
        {selectedModels.size > 1 && <span className="warn"><CircleAlert size={13} />{t.mixedModels}</span>}
        {selectedModels.size === 1 && selectedVersions.size > 1 && <span className="muted"><CircleAlert size={13} />{t.mixedVersions}</span>}
      </div>
      <div className="selection-roles">{principalId && <FoundryAccessNotice accountIds={selectedAccounts} />}</div>
      <div className="selection-actions">
        <Button disabled={busy || selectedModels.size > 1} onClick={() => { importViews.reset(); setAddOpen(true) }}>
          <ListPlus size={13} />{t.addToPool}…
        </Button>
        <Button variant="primary" disabled={busy || selectedViews.length < 2 || selectedModels.size > 1} onClick={() => importViews.mutate({ views: selectedViews, createPool: true })}>
          <Network size={13} />{t.importAndCreatePool}
        </Button>
      </div>
    </div>}
    {addOpen && selectedViews.length > 0 && <AddToPoolDialog
      views={selectedViews}
      deployments={all}
      busy={busy}
      error={importViews.error}
      onConfirm={(poolId) => importViews.mutate({ views: selectedViews, addToPool: poolId })}
      onClose={() => setAddOpen(false)}
    />}
  </div>
}

function AddToPoolDialog({
  views,
  deployments,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  views: DeploymentView[]
  deployments: DeploymentView[]
  busy: boolean
  error: unknown
  onConfirm: (poolId: string) => void
  onClose: () => void
}) {
  const { product, usePools } = useProductUi()
  const { t } = useI18n(product)
  const pools = usePools()
  const [choice, setChoice] = useState<string | null>(null)
  const model = modelName(views[0]!)
  // Imported deployments tell which model each existing member backend serves.
  const modelByBackend = new Map(deployments.flatMap((view) =>
    view.backend_id ? [[view.backend_id.toLowerCase(), modelName(view)] as const] : []))
  const candidates = (pools.data ?? [])
    .filter((pool) => !pool.protected)
    .map((pool) => {
      const memberIds = new Set(pool.members.map((member) => member.backend_id.toLowerCase()))
      const models = new Set(pool.members.flatMap((member) => modelByBackend.get(member.backend_id.toLowerCase()) ?? []))
      const added = views.filter((view) => !view.backend_id || !memberIds.has(view.backend_id.toLowerCase())).length
      const otherModel = [...models].some((candidate) => candidate !== model)
      return {
        pool,
        sameModel: models.has(model),
        otherModel,
        added,
        // Saving would be refused for a pool that already serves another model.
        usable: added > 0 && !otherModel && pool.members.length + added <= MAX_MEMBERS,
      }
    })
    .sort((left, right) => Number(right.sameModel) - Number(left.sameModel) || left.pool.id.localeCompare(right.pool.id))
  const chosen = choice ?? candidates.find((candidate) => candidate.usable)?.pool.id ?? null

  return <Dialog
    open
    onClose={() => { if (!busy) onClose() }}
    title={`${t.addToPool} · ${views[0]!.model_name}`}
    description={t.addToPoolHint}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={busy || !chosen} onClick={() => chosen && onConfirm(chosen)}>
        {busy ? <LoaderCircle className="spin" size={13} /> : <ListPlus size={13} />}{busy ? t.importing : t.importAndOpenPool}
      </Button>
    </>}
  >
    {pools.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
      : pools.error ? <ErrorText error={pools.error} />
      : !candidates.length ? <div className="state">{t.noWritablePools}</div>
      : <div className="pool-choices" role="radiogroup" aria-label={t.targetPool}>
        {candidates.map(({ pool, sameModel, otherModel, added, usable }) =>
          <label key={pool.id} className="pool-choice" data-selected={chosen === pool.id} data-disabled={!usable}>
            <input type="radio" name="target-pool" checked={chosen === pool.id} disabled={!usable || busy} onChange={() => setChoice(pool.id)} />
            <span className="list-copy">
              <b>{pool.title || pool.id}</b>
              <small><code>{pool.id}</code><i>·</i>{pool.members.length}<i>·</i>{t[deploymentMode(primaryFirst(pool.members))]}</small>
            </span>
            <span className="pool-choice-tags">
              {sameModel && <span className="tag" data-tone="ok">{t.sameModel}</span>}
              {otherModel && <span className="tag" data-tone="warn">{t.otherModelBlocked}</span>}
              {!otherModel && <small className={usable ? "muted" : "warn"}>
                {added === 0 ? t.allInPool : !usable ? t.poolFull : fill(t.newMembers, { count: added })}
              </small>}
            </span>
          </label>)}
      </div>}
    <ErrorText error={error} />
  </Dialog>
}
