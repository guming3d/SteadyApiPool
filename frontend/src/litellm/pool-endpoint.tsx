// The pool's endpoint: the gateway's OpenAI-compatible base URL and the model names that reach
// the pool (its ID, plus aliases such as the deployment name clients already send), with a look
// at the pool as LiteLLM config.yaml.
import { FileCode2, LoaderCircle, Plus, Tag, X } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { fill, useI18n } from "../i18n"
import { MODEL_NAME_PATTERN } from "./pool-ids"
import type { PoolEndpoint, PoolView } from "./types"
import { CopyButton } from "./call-guide"
import { ConfigDialog } from "./config-viewer"
import { Button, ConfirmDialog, ErrorText, Field } from "../components/ui"

/** Where clients send requests: <gateway>/v1, or a placeholder until the gateway has a URL. */
export function baseUrlOf(endpoint: Pick<PoolEndpoint, "base_url"> | undefined, gatewayUrl?: string | null) {
  return endpoint?.base_url ?? `${(gatewayUrl || "https://<gateway>").replace(/\/+$/, "")}/v1`
}

export function PoolEndpointPanel({ pool, canWrite }: { pool: PoolView; canWrite: boolean }) {
  const { t, locale } = useI18n("litellm")
  const queryClient = useQueryClient()
  const endpoint = useQuery(queries.endpoint(pool.id))
  // Every alias on the gateway, so a name another pool uses is caught before saving.
  const aliases = useQuery({ ...queries.aliases(), enabled: canWrite })
  const pools = useQuery(queries.pools())
  const [name, setName] = useState("")
  const [removing, setRemoving] = useState<string | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  const refresh = (next: PoolEndpoint) => {
    queryClient.setQueryData(keys.endpoint(pool.id), next)
    void queryClient.invalidateQueries({ queryKey: keys.aliases })
    void queryClient.invalidateQueries({ queryKey: keys.pool(pool.id) })
    void queryClient.invalidateQueries({ queryKey: keys.poolConfig(pool.id) })
    // The test key is scoped to the pool's names; a new key is needed for a new name.
    void queryClient.invalidateQueries({ queryKey: keys.poolKeys(pool.id) })
  }
  const add = useMutation({
    mutationFn: () => api.addAlias(pool.id, name.trim()),
    onSuccess: (next) => { setName(""); refresh(next) },
  })
  const remove = useMutation({
    mutationFn: (alias: string) => api.removeAlias(pool.id, alias),
    onSuccess: (next) => { setRemoving(null); refresh(next) },
  })

  const trimmed = name.trim()
  const lowered = trimmed.toLowerCase()
  const takenBy = (aliases.data ?? []).find((alias) => alias.name.toLowerCase() === lowered)?.pool_id
    ?? (pools.data ?? []).find((item) => item.id.toLowerCase() === lowered)?.id
  const nameError = !trimmed ? null
    : !MODEL_NAME_PATTERN.test(trimmed) ? t.aliasInvalid
    : takenBy ? fill(t.aliasTaken, { pool: takenBy }, locale)
    : null
  const baseUrl = baseUrlOf(endpoint.data)
  const managed = pool.managed && !pool.protected

  return <section className="endpoint-panel">
    <header className="section-header">
      <b className="call-title"><Tag size={14} />{t.endpointTitle}</b>
      <Button variant="ghost" onClick={() => setConfigOpen(true)}><FileCode2 size={13} />{t.viewConfig}</Button>
    </header>
    <p className="muted">{t.endpointHint}</p>
    {endpoint.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>}
    <ErrorText error={endpoint.error} />
    {endpoint.data && <>
      <dl className="call-facts">
        <div><dt>{t.baseUrl}</dt><dd><code>{baseUrl}</code><CopyButton text={baseUrl} label={t.baseUrl} /></dd></div>
        <div><dt>{t.modelNames}</dt><dd className="model-names">
          {endpoint.data.model_names.map((model, index) => <span key={model} className="alias-chip" data-primary={index === 0}>
            <code>{model}</code>
            <CopyButton text={model} label={t.modelName} />
            {index > 0 && canWrite && managed && <button type="button" className="icon-button" title={t.aliasRemove} aria-label={`${t.aliasRemove}: ${model}`} onClick={() => setRemoving(model)}><X size={12} /></button>}
          </span>)}
        </dd></div>
      </dl>
      {canWrite && managed && <div className="alias-add">
        <Field label={t.aliasAdd} hint={t.aliasHint}>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="gpt-4o" aria-invalid={Boolean(nameError)} spellCheck={false} />
        </Field>
        <Button disabled={!trimmed || Boolean(nameError) || add.isPending} onClick={() => add.mutate()}>
          {add.isPending ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{t.aliasAddButton}
        </Button>
        {nameError && <small className="warn-text">{nameError}</small>}
      </div>}
      <ErrorText error={add.error ?? remove.error} />
    </>}
    {configOpen && <ConfigDialog poolId={pool.id} onClose={() => setConfigOpen(false)} />}
    {removing && <ConfirmDialog
      open
      title={t.aliasRemove}
      description={fill(t.aliasRemoveConfirm, { alias: removing, pool: pool.title || pool.id }, locale)}
      confirmLabel={t.aliasRemove}
      busy={remove.isPending}
      error={remove.error}
      onConfirm={() => remove.mutate(removing)}
      onClose={() => setRemoving(null)}
    />}
  </section>
}
