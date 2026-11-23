// A pool member that isn't a discovered Foundry deployment: an OpenAI-compatible endpoint (its
// base URL) and the model it serves, reached with an API key the gateway keeps as a secret
// header. The app gives it its own model provider in the gateway. Opened from a pool (add or
// edit); the failure rule and retries are the pool's, so they aren't edited here.
import { LoaderCircle } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { useI18n } from "../i18n"
import type { AuthType, BackendView, MemberBackendWrite } from "./types"
import { Button, Dialog, ErrorText, Field } from "../components/ui"

const HEADER_NAME = /^[A-Za-z0-9-]{1,64}$/

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
  const { t } = useI18n("aigateway")
  const queryClient = useQueryClient()
  const capabilities = useQuery(queries.capabilities())
  const creating = backend === null
  const [id, setId] = useState(backend?.id ?? capabilities.data?.id_prefix ?? "")
  const [title, setTitle] = useState(backend?.title ?? "")
  const [url, setUrl] = useState(backend?.url ?? "")
  const [model, setModel] = useState(backend?.model ?? "")
  const [authType, setAuthType] = useState<AuthType>(backend?.auth_type === "bearer" ? "bearer" : "api_key")
  const [headerName, setHeaderName] = useState(backend?.auth_type === "api_key" && backend.header_name ? backend.header_name : "api-key")
  const [secret, setSecret] = useState("")
  // Changing how the key is sent needs the key again; the gateway never returns it.
  const sameAuth = !creating && backend.auth_type === authType
    && (authType === "bearer" || (backend.header_name ?? "").toLowerCase() === headerName.toLowerCase())

  const save = useMutation({
    mutationFn: async () => {
      // The list carries no ETags; an update reads the member's own first.
      const etag = creating ? null : (await api.backend(backend.id)).etag
      const body: MemberBackendWrite = {
        title: title.trim() || null,
        url: url.trim(),
        model: model.trim(),
        auth: { type: authType, header_name: headerName.trim(), secret: secret || null },
        etag,
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
  const valid = /^[A-Za-z0-9][A-Za-z0-9-]{0,79}$/.test(id.trim()) && /^https:\/\//.test(url.trim())
    && model.trim() !== ""
    && (authType === "bearer" || HEADER_NAME.test(headerName.trim()))
    && (secret !== "" || sameAuth)

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
      <div className="span-2"><Field label={t.url} hint={t.memberUrlHint}><input value={url} placeholder="https://api.contoso.com/v1" onChange={(event) => setUrl(event.target.value)} /></Field></div>
      <div className="span-2"><Field label={t.memberModel} hint={t.memberModelHint}><input value={model} placeholder="gpt-4o" onChange={(event) => setModel(event.target.value)} /></Field></div>
      <Field label={t.auth}>
        <select value={authType} onChange={(event) => setAuthType(event.target.value as AuthType)}>
          <option value="api_key">{t.authApiKey}</option>
          <option value="bearer">{t.authBearer}</option>
        </select>
      </Field>
      {authType === "api_key"
        ? <Field label={t.headerName}><input value={headerName} spellCheck={false} aria-invalid={!HEADER_NAME.test(headerName.trim())} onChange={(event) => setHeaderName(event.target.value)} /></Field>
        : <span />}
      <div className="span-2"><Field label={t.secretValue} hint={sameAuth ? t.keepSecret : t.secretValueHint}>
        <input type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} />
      </Field></div>
      <div className="state span-2">{t.memberBreakerFromPool}</div>
    </div>
    <ErrorText error={save.error} />
  </Dialog>
}
