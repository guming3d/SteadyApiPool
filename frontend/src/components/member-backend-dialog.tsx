// A pool member that isn't a discovered Foundry deployment: an Azure OpenAI deployment URL, or any
// OpenAI-compatible endpoint with the model it serves, reached with an API key, the gateway's
// managed identity or nothing. Opened from a pool (add or edit); cooldown and retries are the
// pool's, so they aren't edited here.
import { LoaderCircle } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { useI18n } from "../i18n"
import type { AuthType, BackendView, MemberBackendWrite } from "../types"
import { Button, Dialog, ErrorText, Field } from "./ui"

/** An Azure OpenAI deployment URL, which names its own deployment; anything else needs a model. */
export const isAzureDeploymentUrl = (url: string) => /\/openai\/deployments\/[^/]+\/?$/.test(url.trim())

export function MemberBackendDialog({
  backend,
  onSaved,
  onClose,
}: {
  /** null creates a new member. */
  backend: BackendView | null
  onSaved?: (backend: BackendView) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const capabilities = useQuery(queries.capabilities())
  const creating = backend === null
  const [id, setId] = useState(backend?.id ?? capabilities.data?.id_prefix ?? "")
  const [title, setTitle] = useState(backend?.title ?? "")
  const [url, setUrl] = useState(backend?.url ?? "")
  const [model, setModel] = useState(backend?.provider === "openai" ? backend.model : "")
  const [authType, setAuthType] = useState<AuthType>(
    backend && backend.auth_type !== "custom" ? backend.auth_type : "api_key",
  )
  const [secret, setSecret] = useState("")
  const azure = isAzureDeploymentUrl(url)
  // An API-key member already in a pool keeps the key the gateway stored for it.
  const canKeepSecret = !creating && backend.auth_type === "api_key" && backend.pools.length > 0

  const save = useMutation({
    mutationFn: () => {
      const body: MemberBackendWrite = {
        title: title.trim() || null,
        url: url.trim(),
        model: azure ? null : model.trim() || null,
        auth: { type: authType, secret: authType === "api_key" && secret ? secret : null },
      }
      return api.saveBackend(id.trim(), body)
    },
    onSuccess: async (saved) => {
      // The pool editor resolves members against this list, so wait for the new one.
      await queryClient.refetchQueries({ queryKey: keys.backends })
      onSaved?.(saved)
      onClose()
    },
  })
  const valid = /^[A-Za-z0-9][A-Za-z0-9-]{0,79}$/.test(id.trim()) && /^https?:\/\//.test(url.trim())
    && (azure || model.trim() !== "")
    && (authType !== "api_key" || secret !== "" || canKeepSecret)

  return <Dialog
    open
    wide
    onClose={() => { if (!save.isPending) onClose() }}
    title={creating ? t.addBackend : `${t.editEndpoint}: ${backend.id}`}
    description={creating ? t.customEndpointHint : undefined}
    footer={<>
      <Button disabled={save.isPending} onClick={onClose}>{t.cancel}</Button>
      <Button variant="primary" disabled={!valid || save.isPending} onClick={() => save.mutate()}>
        {save.isPending && <LoaderCircle className="spin" size={13} />}{save.isPending ? t.saving : creating ? t.create : t.save}
      </Button>
    </>}
  >
    <div className="form-grid">
      <Field label={t.backendId} hint={t.idHint}><input value={id} disabled={!creating} onChange={(event) => setId(event.target.value)} autoFocus={creating} /></Field>
      <Field label={t.title}><input value={title} onChange={(event) => setTitle(event.target.value)} /></Field>
      <div className="span-2"><Field label={t.url} hint={t.memberUrlHint}><input value={url} placeholder="https://my-aoai.openai.azure.com/openai/deployments/gpt-4o" onChange={(event) => setUrl(event.target.value)} /></Field></div>
      {!azure && <div className="span-2"><Field label={t.memberModel} hint={t.memberModelHint}><input value={model} placeholder="gpt-4o" onChange={(event) => setModel(event.target.value)} /></Field></div>}
      <Field label={t.auth}>
        <select value={authType} onChange={(event) => setAuthType(event.target.value as AuthType)}>
          <option value="api_key">{t.authApiKey}</option>
          {azure && <option value="managed_identity">{t.authManagedIdentity}</option>}
          <option value="none">{t.authNone}</option>
        </select>
      </Field>
      {authType === "api_key"
        ? <Field label={t.secretValue} hint={canKeepSecret ? t.keepSecret : t.secretValueHint}><input type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} /></Field>
        : <span />}
      {authType === "managed_identity" && <div className="state span-2">{t.miResourceHint}</div>}
      <div className="state span-2">{t.memberBreakerFromPool}</div>
    </div>
    <ErrorText error={save.error} />
  </Dialog>
}
