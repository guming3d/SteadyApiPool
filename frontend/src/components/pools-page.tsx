// The pools page: the list on the left, the selected pool's editor on the right.
import { Network, Plus, RefreshCw, Search, ShieldCheck } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "../api"
import { useI18n } from "../i18n"
import { deploymentMode, primaryFirst } from "../pool-modes"
import type { PoolApiCreate } from "../types"
import { PoolEditor, type PoolAddRequest, type PoolCreateRequest } from "./pool-editor"
import { Button, Dialog, ErrorText } from "./ui"

export function PoolsPage({
  createRequest,
  onCreateRequestHandled,
  addRequest,
  onAddRequestHandled,
}: {
  /** Opens the create dialog prefilled, e.g. after importing deployments. */
  createRequest?: PoolCreateRequest | null
  onCreateRequestHandled?: () => void
  /** Opens an existing pool with members added to its draft, e.g. after importing deployments. */
  addRequest?: PoolAddRequest | null
  onAddRequestHandled?: () => void
}) {
  const { t } = useI18n()
  const me = useQuery(queries.me())
  const pools = useQuery(queries.pools())
  const [search, setSearch] = useState("")
  // Start on the requested pool, so the select-the-first-pool fallback below never overrides it.
  const [selectedId, setSelectedId] = useState<string | null>(addRequest?.poolId ?? null)
  const [pendingAdd, setPendingAdd] = useState<PoolAddRequest | null>(addRequest ?? null)
  // The API a just-created pool should get; its editor creates and binds it once.
  const [pendingApi, setPendingApi] = useState<{ poolId: string; api: PoolApiCreate } | null>(null)
  const clearPendingApi = useCallback(() => setPendingApi(null), [])
  const [createOpen, setCreateOpen] = useState(false)
  const [prefill, setPrefill] = useState<PoolCreateRequest | undefined>()
  useEffect(() => {
    if (!createRequest) return
    setPrefill(createRequest)
    setCreateOpen(true)
    onCreateRequestHandled?.()
  }, [createRequest, onCreateRequestHandled])
  useEffect(() => {
    if (!addRequest) return
    setSelectedId(addRequest.poolId)
    setPendingAdd(addRequest)
    onAddRequestHandled?.()
  }, [addRequest, onAddRequestHandled])
  const select = (poolId: string | null) => {
    setPendingAdd(null)
    setPendingApi(null)
    setSelectedId(poolId)
  }
  const closeCreate = () => { setCreateOpen(false); setPrefill(undefined) }

  const visible = useMemo(() => {
    const normalized = search.trim().toLowerCase()
    return (pools.data ?? []).filter((pool) =>
      !normalized || `${pool.id} ${pool.title ?? ""}`.toLowerCase().includes(normalized))
  }, [pools.data, search])

  // Select a new pool only once the refetched list has it,
  // otherwise the fallback below would snap back to the first pool.
  const [pendingId, setPendingId] = useState<string | null>(null)
  useEffect(() => {
    if (!pendingId || !pools.data?.some((pool) => pool.id === pendingId)) return
    setSelectedId(pendingId)
    setPendingId(null)
  }, [pendingId, pools.data])

  useEffect(() => {
    if (!pools.data || pendingId) return
    if (pools.data.some((pool) => pool.id === selectedId)) return
    setSelectedId(pools.data[0]?.id ?? null)
  }, [pendingId, pools.data, selectedId])

  const selected = pools.data?.find((pool) => pool.id === selectedId) ?? null

  return <div className="page">
    <header className="page-header">
      <div><h1>{t.pools}</h1><span>{pools.data?.length ?? "—"}</span></div>
      {me.data?.can_write && <Button variant="primary" data-tour="add-pool" onClick={() => setCreateOpen(true)}><Plus size={14} />{t.addPool}</Button>}
    </header>
    {pools.isLoading ? <div className="state"><RefreshCw className="spin" size={14} />{t.loadingPools}</div>
      : pools.error ? <ErrorText error={pools.error} />
      : <div className="split">
        <aside className="list-pane" data-tour="pool-list">
          <label className="search"><Search size={14} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t.searchPools} aria-label={t.searchPools} /></label>
          <div className="list" role="list">
            {visible.map((pool) => <button type="button" role="listitem" key={pool.id} className={`list-row ${pool.id === selectedId ? "active" : ""}`} data-tour={pool.id === selectedId ? "pool-row" : undefined} onClick={() => select(pool.id)}>
              <span className="list-icon">{pool.protected ? <ShieldCheck size={15} /> : <Network size={15} />}</span>
              <span className="list-copy">
                <b title={pool.title ?? pool.id}>{pool.title || pool.id}</b>
                <small><code>{pool.id}</code><i>·</i>{pool.members.length}<i>·</i>{t[deploymentMode(primaryFirst(pool.members))]}</small>
              </span>
            </button>)}
            {!visible.length && <div className="empty"><b>{pools.data?.length ? t.noMatch : t.noPools}</b><span>{pools.data?.length ? "" : t.noPoolsHint}</span></div>}
          </div>
        </aside>
        <section className="detail-pane">
          {selected ? <PoolEditor
            key={selected.id}
            poolId={selected.id}
            addMembers={pendingAdd?.poolId === selected.id ? pendingAdd.members : undefined}
            autoCreateApi={pendingApi?.poolId === selected.id ? pendingApi.api : undefined}
            onAutoCreateApiStarted={clearPendingApi}
            onDeleted={() => select(null)}
            header={(controls) => <header className="detail-toolbar">
              <div><b>{selected.title || selected.id}</b><code>{selected.id}</code></div>
              {controls}
            </header>}
          /> : <div className="empty"><Network size={28} /><b>{t.selectPool}</b><span>{t.selectPoolHint}</span></div>}
        </section>
      </div>}
    <Dialog open={createOpen} wide onClose={closeCreate} title={t.addPool} description={t.noPoolsHint}>
      {createOpen && <PoolEditor initial={prefill} onCreated={(poolId, api) => { closeCreate(); setPendingId(poolId); setPendingApi(api ? { poolId, api } : null) }} />}
    </Dialog>
  </div>
}
