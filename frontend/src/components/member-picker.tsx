// Adds members to a pool draft: discovered deployments of the pool's model (imported on the way
// in), members of other pools that are not Foundry deployments, or a new custom endpoint.
import { CircleAlert, ListPlus, LoaderCircle, Plus, RefreshCw } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import type { DeploymentView } from "../types"
import { modelName, refKey } from "./deployments-page"
import { MemberBackendDialog } from "./member-backend-dialog"
import { Button, Dialog, ErrorText } from "./ui"

export function AddMembersDialog({
  model,
  memberIds,
  room,
  onAdd,
  onClose,
}: {
  /** The pool's model as read from Azure; null while the pool has no Foundry member. */
  model: string | null
  /** Lower-case backend IDs already in the draft. */
  memberIds: ReadonlySet<string>
  /** How many members can still be added (30 max). */
  room: number
  onAdd: (backendIds: string[]) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const deployments = useQuery(queries.deployments())
  const backends = useQuery(queries.backends())
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [pickedBackends, setPickedBackends] = useState<Set<string>>(new Set())
  const [customOpen, setCustomOpen] = useState(false)
  const [othersOpen, setOthersOpen] = useState(false)

  const refresh = useMutation({
    mutationFn: () => api.deployments(true),
    onSuccess: (data) => queryClient.setQueryData(keys.deployments, data),
  })
  const add = useMutation({
    mutationFn: async (views: DeploymentView[]) => {
      if (!views.length) return []
      const result = await api.importDeployments(views.map(({ account_id, deployment }) => ({ account_id, deployment })))
      // The editor resolves members against the backend list, so wait for the new ones.
      await queryClient.refetchQueries({ queryKey: keys.backends })
      void queryClient.invalidateQueries({ queryKey: keys.deployments })
      return result.backend_ids
    },
    onSuccess: (backendIds) => onAdd([...backendIds, ...pickedBackends]),
  })

  const all = deployments.data ?? []
  const pickedViews = all.filter((view) => picked.has(refKey(view)))
  // Without a pool model yet, the first pick decides it.
  const lockedModel = model?.toLowerCase() ?? (pickedViews[0] ? modelName(pickedViews[0]) : null)
  const inPool = (view: DeploymentView) => Boolean(view.backend_id && memberIds.has(view.backend_id.toLowerCase()))
  const candidates = all
    .filter((view) => !model || modelName(view) === model.toLowerCase())
    // Each model's deployments together, then the ones not in the pool yet first.
    .sort((left, right) => modelName(left).localeCompare(modelName(right))
      || Number(inPool(left)) - Number(inPool(right))
      || left.account_name.localeCompare(right.account_name) || left.deployment.localeCompare(right.deployment))
  const deploymentBackendIds = new Set(all.flatMap((view) => view.backend_id ? [view.backend_id.toLowerCase()] : []))
  // Rows this app didn't write can't join a pool (the backend refuses them).
  const others = (backends.data ?? []).filter((backend) => backend.managed
    && !memberIds.has(backend.id.toLowerCase()) && !deploymentBackendIds.has(backend.id.toLowerCase()))
  const count = picked.size + pickedBackends.size
  const full = count >= room
  const busy = add.isPending

  const toggle = (set: Set<string>, setter: (next: Set<string>) => void, key: string, on: boolean) => {
    const next = new Set(set)
    if (on) next.add(key)
    else next.delete(key)
    setter(next)
  }

  return <Dialog
    open
    wide
    onClose={() => { if (!busy) onClose() }}
    title={model ? fill(t.addDeployments, { model }) : t.addMembersTitle}
    description={model ? fill(t.sameModelOnly, { model }) : t.pickOneModel}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={busy || count === 0} onClick={() => add.mutate(pickedViews)}>
        {busy ? <LoaderCircle className="spin" size={13} /> : <ListPlus size={13} />}
        {busy ? t.importing : `${t.addToThisPool}${count ? ` (${count})` : ""}`}
      </Button>
    </>}
  >
    <div className="picker-toolbar">
      {full && <span className="warn"><CircleAlert size={13} />{t.poolFull}</span>}
      <Button variant="ghost" disabled={busy || full} title={t.customEndpointHint} onClick={() => setCustomOpen(true)}>
        <Plus size={13} />{t.customEndpoint}
      </Button>
      <Button variant="ghost" disabled={refresh.isPending || deployments.isFetching} onClick={() => refresh.mutate()}>
        <RefreshCw size={13} className={refresh.isPending ? "spin" : ""} />{t.refresh}
      </Button>
    </div>
    {deployments.isLoading || refresh.isPending ? <div className="state"><LoaderCircle className="spin" size={14} />{t.scanning}</div>
      : deployments.error ? <ErrorText error={deployments.error} />
      : !candidates.some((view) => !inPool(view)) ? <div className="state">{model ? fill(t.noSameModelDeployments, { model }) : t.noDeployments}</div>
      : null}
    {candidates.length > 0 && <div className="card picker-table"><table className="table">
      <tbody>{candidates.map((view) => {
        const key = refKey(view)
        const member = inPool(view)
        const otherModel = lockedModel !== null && modelName(view) !== lockedModel
        const checked = member || picked.has(key)
        return <tr key={key} data-selected={checked} data-disabled={member || otherModel}>
          <td className="check-cell"><input
            type="checkbox"
            checked={checked}
            disabled={busy || member || otherModel || (!checked && full)}
            title={otherModel ? t.otherModelBlocked : undefined}
            aria-label={`${view.account_name} ${view.deployment}`}
            onChange={(event) => toggle(picked, setPicked, key, event.target.checked)}
          /></td>
          <td><b>{view.deployment}</b><br /><code className="muted" title={view.backend_url}>{view.account_name}</code></td>
          <td>{!model && <><span className="model-tag">{view.model_name} {view.model_version}</span><br /></>}{view.location}</td>
          <td>{view.sku}{view.capacity !== null && <small className="muted"> · {view.capacity}</small>}</td>
          <td className="actions">{member ? <span className="muted">{t.inThisPool}</span> : view.backend_id ? <span className="imported">{t.imported}</span> : <span className="muted">{t.notImported}</span>}</td>
        </tr>
      })}</tbody>
    </table></div>}
    {others.length > 0 && <details className="picker-others" open={othersOpen} onToggle={(event) => setOthersOpen(event.currentTarget.open)}>
      <summary>{t.otherBackends} ({others.length})</summary>
      <div className="card picker-table"><table className="table"><tbody>{others.map((backend) => {
        const checked = pickedBackends.has(backend.id)
        return <tr key={backend.id} data-selected={checked}>
          <td className="check-cell"><input type="checkbox" checked={checked} disabled={busy || (!checked && full)} aria-label={backend.id} onChange={(event) => toggle(pickedBackends, setPickedBackends, backend.id, event.target.checked)} /></td>
          <td><b>{backend.title || backend.id}</b><br /><code className="muted">{backend.url}</code></td>
        </tr>
      })}</tbody></table></div>
    </details>}
    <ErrorText error={add.error ?? refresh.error} />
    {/* A new endpoint lands ticked in the list above, alongside any other picks. */}
    {customOpen && <MemberBackendDialog
      backend={null}
      onSaved={(saved) => {
        setPickedBackends((current) => new Set(current).add(saved.id))
        setOthersOpen(true)
      }}
      onClose={() => setCustomOpen(false)}
    />}
  </Dialog>
}
