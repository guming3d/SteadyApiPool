// Removing a pool with its aliases and test key, and cleaning up everything this app created on
// the gateway. Both read a plan first and confirm exactly that plan; see PoolManagerService._remove.
import { CircleAlert, KeyRound, LoaderCircle, ShieldCheck, Tag, Trash2 } from "lucide-react"
import { useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { fill, useI18n } from "../i18n"
import { confirmPlan, nameMatches, planIsEmpty } from "./removal"
import type { RemovalAlias, RemovalKey, RemovalPlan } from "./types"
import { Button, Dialog, ErrorText } from "../components/ui"

function Group({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return <li>{icon}<div><b>{title}</b><ul>{children}</ul></div></li>
}

const aliasRow = (item: RemovalAlias) => <li key={item.name}><code>{item.name}</code> → <code>{item.pool_id}</code></li>
const keyRow = (item: RemovalKey) => <li key={item.id}><code>{item.alias ?? item.id}</code></li>
const idRow = (id: string) => <li key={id}><code>{id}</code></li>

/** Every change a plan makes, grouped as the backend runs them. */
export function RemovalPlanView({ plan }: { plan: RemovalPlan }) {
  const { t } = useI18n("litellm")
  const names = plan.delete_aliases.length + plan.unbind_aliases.length
  return <>
    {plan.blocked.length > 0 && <div className="callout warning"><CircleAlert size={15} /><div>
      <b>{t.removalBlocked}</b>
      {plan.blocked.map((reason) => <span key={reason}>{reason}</span>)}
    </div></div>}
    <ul className="removal-plan">
      {plan.unbind_aliases.length > 0 && <Group icon={<Tag size={14} />} title={t.removalUnbindAliases}>{plan.unbind_aliases.map(aliasRow)}</Group>}
      {plan.delete_aliases.length > 0 && <Group icon={<Tag size={14} />} title={t.removalDeleteAliases}>{plan.delete_aliases.map(aliasRow)}</Group>}
      {plan.delete_keys.length > 0 && <Group icon={<KeyRound size={14} />} title={t.removalDeleteKeys}>{plan.delete_keys.map(keyRow)}</Group>}
      {plan.pools.length > 0 && <Group icon={<Trash2 size={14} />} title={t.removalDeletePools}>{plan.pools.map(idRow)}</Group>}
      {plan.kept_pools.length > 0 && <Group icon={<ShieldCheck size={14} />} title={t.removalKeptPools}>{plan.kept_pools.map(idRow)}</Group>}
    </ul>
    {(plan.pools.length > 0 || names > 0) && <p className="muted">{t.removalClients}</p>}
  </>
}

/** Everything depends on what was removed: pools, aliases, keys, members, discovery and setup. */
function useRemoved() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries()
}

export function RemovePoolDialog({ poolId, etag, onClose, onRemoved }: {
  poolId: string
  etag: string | null | undefined
  onClose: () => void
  onRemoved: () => void
}) {
  const { t } = useI18n("litellm")
  const queryClient = useQueryClient()
  const removed = useRemoved()
  const plan = useQuery(queries.poolRemoval(poolId))
  const remove = useMutation({
    mutationFn: (value: RemovalPlan) => api.removePool(poolId, confirmPlan(value, etag)),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: keys.pool(poolId) })
      onRemoved()
      await removed()
    },
    // A partial removal changed things too; show what is left.
    onError: () => { void plan.refetch() },
  })
  const busy = remove.isPending
  return <Dialog
    open
    onClose={() => { if (!busy) onClose() }}
    title={fill(t.removePoolTitle, { pool: poolId })}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="destructive" disabled={!plan.data || plan.data.blocked.length > 0 || busy} onClick={() => plan.data && remove.mutate(plan.data)}>
        {busy ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}{busy ? t.deleting : t.delete}
      </Button>
    </>}
  >
    <div className="removal">
      {plan.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.removalLoading}</div>}
      <ErrorText error={plan.error} />
      {plan.data && <RemovalPlanView plan={plan.data} />}
      <ErrorText error={remove.error} />
    </div>
  </Dialog>
}

export function CleanupDialog({ gatewayName, onClose }: { gatewayName: string; onClose: () => void }) {
  const { t, locale } = useI18n("litellm")
  const removed = useRemoved()
  const [typed, setTyped] = useState("")
  const plan = useQuery(queries.cleanup())
  const cleanup = useMutation({
    mutationFn: (value: RemovalPlan) => api.cleanup({ ...confirmPlan(value), confirm_name: typed.trim() }),
    onSuccess: () => removed(),
    onError: () => { void plan.refetch() },
  })
  const busy = cleanup.isPending
  const empty = plan.data ? planIsEmpty(plan.data) : false
  const done = cleanup.data
  return <Dialog
    open
    onClose={() => { if (!busy) onClose() }}
    title={fill(t.cleanupTitle, { name: gatewayName })}
    footer={done || empty
      ? <Button onClick={onClose}>{t.close}</Button>
      : <>
        <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
        <Button variant="destructive" disabled={!plan.data || plan.data.blocked.length > 0 || !nameMatches(typed, gatewayName) || busy} onClick={() => plan.data && cleanup.mutate(plan.data)}>
          {busy ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}{t.cleanupConfirm}
        </Button>
      </>}
  >
    <div className="removal">
      {done
        ? <div className="state" data-status="active">{fill(t.removalDone, {
          pools: done.pools.length,
          aliases: done.deleted_aliases.length + done.unbound_aliases.length,
          keys: done.deleted_keys.length,
        }, locale)}</div>
        : <>
          <p className="muted">{t.cleanupHint}</p>
          {plan.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.removalLoading}</div>}
          <ErrorText error={plan.error} />
          {empty && <div className="state">{t.cleanupEmpty}</div>}
          {plan.data && !empty && <>
            <RemovalPlanView plan={plan.data} />
            <label className="field">{fill(t.deleteConfirmLabel, { name: gatewayName })}
              <input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} placeholder={gatewayName} disabled={busy} />
            </label>
          </>}
        </>}
      <ErrorText error={cleanup.error} />
    </div>
  </Dialog>
}
