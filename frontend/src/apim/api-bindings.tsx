import { Check, Copy, Eye, FileCode2, Link2, Link2Off, LoaderCircle, Plus, RotateCcw } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { fill, useI18n } from "../i18n"
import { API_PATH_PATTERN, suggestApiPath } from "./api-paths"
import type { ApiBindingWrite, ApiView, PoolApiCreate, PoolView } from "./types"
import { PolicyActions, PolicyDialog, PolicyDiff, policyFilename } from "./policy-viewer"
import { Button, ConfirmDialog, Dialog, ErrorText, Field } from "../components/ui"

// The app's default retry on 429: two attempts, one second apart, the first at once.
const DEFAULT_BINDING: Omit<ApiBindingWrite, "pool_id"> = {
  retry_on_429: true,
  max_attempts_per_request: 2,
  retry_interval_seconds: 1,
  first_fast_retry: true,
  backend_timeout_seconds: 120,
  managed_identity_resource: null,
}

/** Full URL clients call for an API path. */
export function endpointUrl(gatewayUrl: string | null | undefined, path: string) {
  return `${(gatewayUrl || "https://<apim>.azure-api.net").replace(/\/+$/, "")}/${path}`
}

export function ApiBindings({
  pool,
  canWrite,
  autoCreate,
  onAutoCreateStarted,
}: {
  pool: PoolView
  canWrite: boolean
  /** Created and bound once on mount, e.g. right after the pool itself was created. */
  autoCreate?: PoolApiCreate
  onAutoCreateStarted?: () => void
}) {
  const { t } = useI18n("apim")
  const queryClient = useQueryClient()
  const apis = useQuery(queries.apis())
  const capabilities = useQuery(queries.capabilities())
  const [bindOpen, setBindOpen] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [unbinding, setUnbinding] = useState<ApiView | null>(null)
  const [viewing, setViewing] = useState<ApiView | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const unbind = useMutation({
    mutationFn: (target: ApiView) => api.unbind(target.id, target.etag),
    onSuccess: () => {
      setUnbinding(null)
      void queryClient.invalidateQueries({ queryKey: keys.apis })
    },
  })
  const createApi = useMutation({
    mutationFn: (body: PoolApiCreate) => api.createPoolApi(pool.id, body),
    onSuccess: () => {
      setCreateOpen(false)
      void queryClient.invalidateQueries({ queryKey: keys.apis })
    },
  })
  const pendingCreate = useRef(autoCreate)
  useEffect(() => {
    const body = pendingCreate.current
    if (!body) return
    pendingCreate.current = undefined
    onAutoCreateStarted?.()
    createApi.mutate(body)
  }, [createApi, onAutoCreateStarted])

  const bound = (apis.data ?? []).filter(
    (item) => item.binding?.pool_id.toLowerCase() === pool.id.toLowerCase(),
  )
  const writable = canWrite && !pool.protected
  const gatewayUrl = capabilities.data?.gateway_url
  const copy = async (url: string) => {
    await navigator.clipboard.writeText(url)
    setCopied(url)
    setTimeout(() => setCopied(null), 1500)
  }

  return <section className="bindings">
    <header className="section-header">
      <b>{t.boundApis}</b>
      {writable && <span className="editor-actions">
        <Button onClick={() => { createApi.reset(); setCreateOpen(true) }} disabled={!apis.data || createApi.isPending}><Plus size={13} />{t.createApi}</Button>
        <Button onClick={() => setBindOpen(true)} disabled={!apis.data}><Link2 size={13} />{t.bindApi}</Button>
      </span>}
    </header>
    {createApi.isPending && !createOpen && <div className="state"><LoaderCircle className="spin" size={14} />{t.creatingApi}</div>}
    {createApi.error && !createOpen && <div className="state error" role="alert">
      <span>{t.createApiFailed} {createApi.error.message}</span>
      {createApi.variables && <Button variant="ghost" onClick={() => createApi.mutate(createApi.variables!)}><RotateCcw size={13} />{t.retry}</Button>}
    </div>}
    {apis.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
      : apis.error ? <ErrorText error={apis.error} />
      : bound.length === 0 ? !createApi.isPending && <div className="state">{t.noBoundApis}</div>
      : <table className="table">
        <thead><tr><th>{t.api} · {t.endpoint}</th><th>{t.retry429}</th><th>{t.miIdentity}</th><th /></tr></thead>
        <tbody>{bound.map((item) => {
          const url = endpointUrl(gatewayUrl, item.path)
          const resource = item.binding?.managed_identity_resource
          return <tr key={item.id}>
            <td className="api-cell">
              <span className="api-name"><b title={item.id}>{item.display_name}</b>{item.managed && <span className="tag" data-tone="ok">{t.createdHere}</span>}</span>
              <span className="endpoint"><code>{url}</code><button type="button" className="icon-button" title={copied === url ? t.copied : t.copy} aria-label={`${t.copy} ${url}`} onClick={() => void copy(url)}>{copied === url ? <Check size={13} /> : <Copy size={13} />}</button></span>
            </td>
            <td>{item.binding?.retry_on_429 ? <>✓ <span className="muted">×{item.binding.max_attempts_per_request ?? "?"}</span></> : "—"}</td>
            <td>{resource ? <span title={resource}>✓</span> : "—"}</td>
            <td className="actions">
              <Button variant="ghost" onClick={() => setViewing(item)}><FileCode2 size={13} />{t.viewPolicy}</Button>
              {writable && <Button variant="ghost" onClick={() => setUnbinding(item)}><Link2Off size={13} />{t.unbind}</Button>}
            </td>
          </tr>
        })}</tbody>
      </table>}
    {createOpen && <CreateApiDialog
      pool={pool}
      apis={apis.data ?? []}
      gatewayUrl={gatewayUrl}
      busy={createApi.isPending}
      error={createApi.error}
      onCreate={(body) => createApi.mutate(body)}
      onClose={() => setCreateOpen(false)}
    />}
    {bindOpen && <BindDialog pool={pool} apis={apis.data ?? []} onClose={() => setBindOpen(false)} />}
    {viewing && <PolicyDialog api={viewing} onClose={() => setViewing(null)} />}
    <ConfirmDialog
      open={unbinding !== null}
      title={`${t.unbind}: ${unbinding?.display_name ?? ""}`}
      description={t.unbindConfirm}
      confirmLabel={t.unbind}
      busy={unbind.isPending}
      error={unbind.error}
      onConfirm={() => unbinding && unbind.mutate(unbinding)}
      onClose={() => { unbind.reset(); setUnbinding(null) }}
    />
  </section>
}

function BindDialog({ pool, apis, onClose }: { pool: PoolView; apis: ApiView[]; onClose: () => void }) {
  const { t } = useI18n("apim")
  const queryClient = useQueryClient()
  const candidates = apis.filter((item) => item.binding?.pool_id.toLowerCase() !== pool.id.toLowerCase())
  const [apiId, setApiId] = useState(candidates[0]?.id ?? "")
  const [binding, setBinding] = useState(DEFAULT_BINDING)
  const [showPreview, setShowPreview] = useState(false)
  const selected = candidates.find((item) => item.id === apiId)
  const body: ApiBindingWrite = { ...binding, pool_id: pool.id, etag: selected?.etag ?? null }
  // Read-only on the server, so it is a query that follows every change to the form.
  const preview = useQuery({
    queryKey: [...keys.policy(apiId), "preview", body],
    queryFn: () => api.previewBinding(apiId, body),
    enabled: showPreview && apiId !== "",
    placeholderData: keepPreviousData,
    retry: false,
  })
  const bind = useMutation({
    mutationFn: () => api.bind(apiId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.apis })
      onClose()
    },
  })
  const number = (key: "max_attempts_per_request" | "retry_interval_seconds" | "backend_timeout_seconds") =>
    (event: React.ChangeEvent<HTMLInputElement>) => setBinding({ ...binding, [key]: Number(event.target.value) })

  return <Dialog
    open
    wide={showPreview}
    onClose={() => { if (!bind.isPending) onClose() }}
    title={`${t.bindApi} → ${pool.id}`}
    description={t.bindHint}
    footer={<>
      <Button disabled={bind.isPending} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={!apiId || bind.isPending} onClick={() => bind.mutate()}>
        {bind.isPending ? <LoaderCircle className="spin" size={13} /> : <Link2 size={13} />}{t.bindApi}
      </Button>
    </>}
  >
    <div className="form-grid">
      <Field label={t.api}>
        <select value={apiId} onChange={(event) => setApiId(event.target.value)}>
          {candidates.map((item) => <option key={item.id} value={item.id}>
            {item.display_name} (/{item.path}){item.binding ? ` · ${t.boundTo} ${item.binding.pool_id}` : ""}
          </option>)}
        </select>
      </Field>
      {selected?.foreign_backend_selection && <div className="state warning span-2">{t.foreignSelection}</div>}
      <label className="check span-2"><input type="checkbox" checked={binding.retry_on_429} onChange={(event) => setBinding({ ...binding, retry_on_429: event.target.checked })} />{t.retry429}</label>
      {binding.retry_on_429 && <>
        <Field label={t.maxAttempts}><input type="number" min={2} max={10} value={binding.max_attempts_per_request} onChange={number("max_attempts_per_request")} /></Field>
        <Field label={t.retryInterval}><input type="number" min={0} max={30} value={binding.retry_interval_seconds} onChange={number("retry_interval_seconds")} /></Field>
        <Field label={t.backendTimeout}><input type="number" min={1} max={240} value={binding.backend_timeout_seconds} onChange={number("backend_timeout_seconds")} /></Field>
        <label className="check"><input type="checkbox" checked={binding.first_fast_retry} onChange={(event) => setBinding({ ...binding, first_fast_retry: event.target.checked })} />{t.firstFastRetry}</label>
      </>}
      <div className="span-2">
        <Field label={t.miResource} hint={t.miResourceHint}>
          <input value={binding.managed_identity_resource ?? ""} placeholder="https://cognitiveservices.azure.com" onChange={(event) => setBinding({ ...binding, managed_identity_resource: event.target.value.trim() || null })} />
        </Field>
      </div>
    </div>
    <div className="policy-preview">
      <Button variant="ghost" disabled={!apiId} onClick={() => setShowPreview(!showPreview)}><Eye size={13} />{showPreview ? t.hidePreview : t.previewPolicy}</Button>
      {showPreview && <>
        {preview.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
          : preview.error ? <ErrorText error={preview.error} />
          : preview.data && <>
            <div className="policy-preview-head"><small className="muted">{t.policyDiffHint}</small><span><PolicyActions xml={preview.data.proposed} filename={policyFilename(apiId)} /></span></div>
            <PolicyDiff before={preview.data.current} after={preview.data.proposed} />
          </>}
      </>}
    </div>
    <ErrorText error={bind.error} />
  </Dialog>
}

/** Path field shared by the Create API dialog and the new-pool form. */
export function ApiPathField({
  path,
  apis,
  gatewayUrl,
  onChange,
}: {
  path: string
  apis: ApiView[]
  gatewayUrl: string | null | undefined
  onChange: (path: string) => void
}) {
  const { t } = useI18n("apim")
  const taken = apiPathConflict(path, apis)
  const valid = API_PATH_PATTERN.test(path)
  return <Field
    label={t.apiPath}
    hint={taken ? <span className="danger">{fill(t.apiPathTaken, { api: taken.display_name })}</span>
      : valid ? fill(t.createApiWithPoolHint, { url: endpointUrl(gatewayUrl, path) })
      : t.apiPathHint}
  >
    <span className="path-input"><span className="muted">/</span><input value={path} onChange={(event) => onChange(event.target.value.replace(/^\/+/, ""))} aria-invalid={path !== "" && (!valid || taken !== undefined)} /></span>
  </Field>
}

/** The API that already uses this path, compared the way APIM does (case-insensitively). */
export function apiPathConflict(path: string, apis: ApiView[]) {
  const normalized = path.replace(/^\/+|\/+$/g, "").toLowerCase()
  return apis.find((item) => item.path.replace(/^\/+|\/+$/g, "").toLowerCase() === normalized)
}

function CreateApiDialog({
  pool,
  apis,
  gatewayUrl,
  busy,
  error,
  onCreate,
  onClose,
}: {
  pool: PoolView
  apis: ApiView[]
  gatewayUrl: string | null | undefined
  busy: boolean
  error: unknown
  onCreate: (body: PoolApiCreate) => void
  onClose: () => void
}) {
  const { t } = useI18n("apim")
  const capabilities = useQuery(queries.capabilities())
  const [path, setPath] = useState(() => suggestApiPath(pool.title, pool.id, capabilities.data?.id_prefix ?? ""))
  const ready = API_PATH_PATTERN.test(path) && !apiPathConflict(path, apis)
  return <Dialog
    open
    onClose={() => { if (!busy) onClose() }}
    title={`${t.createApi} → ${pool.id}`}
    description={t.createApiHint}
    footer={<>
      <Button disabled={busy} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={busy || !ready} onClick={() => onCreate({ path })}>
        {busy ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{busy ? t.creatingApi : t.createApi}
      </Button>
    </>}
  >
    <ApiPathField path={path} apis={apis} gatewayUrl={gatewayUrl} onChange={setPath} />
    <ErrorText error={error} />
  </Dialog>
}
