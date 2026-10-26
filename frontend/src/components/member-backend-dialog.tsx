// A pool member that isn't a Foundry deployment: any OpenAI-compatible endpoint, reached with an
// API key, a bearer token, APIM's managed identity or nothing. Opened from a pool (add or edit);
// the circuit breaker is the pool's, so it isn't edited here.
import { LoaderCircle } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { useI18n } from "../i18n"
import { DEFAULT_BREAKER } from "../pool-breaker"
import type { AuthType, BackendView, CircuitBreaker, MemberBackendWrite } from "../types"
import { Button, Dialog, ErrorText, Field } from "./ui"

export function MemberBackendDialog({
  backend,
  breaker,
  onSaved,
  onClose,
}: {
  /** null creates a new member. */
  backend: BackendView | null
  /** The pool's rule, given to a new member; publishing the pool keeps it in step. */
  breaker?: CircuitBreaker
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
  const [authType, setAuthType] = useState<AuthType>(
    backend && backend.auth_type !== "custom" ? backend.auth_type : "api_key",
  )
  const [headerName, setHeaderName] = useState(backend?.header_name ?? "api-key")
  const [secretMode, setSecretMode] = useState<"value" | "key_vault">("value")
  const [secret, setSecret] = useState("")
  const [keyVaultId, setKeyVaultId] = useState("")
  const [keyVaultIdentity, setKeyVaultIdentity] = useState("")
  const needsSecret = authType === "api_key" || authType === "bearer"
  const secretProvided = secretMode === "value" ? secret !== "" : keyVaultId.trim() !== ""
  // Editing may keep the stored secret only if the backend already had one of the same kind.
  const canKeepSecret = !creating && backend.named_value !== null
    && (backend.auth_type === "api_key" || backend.auth_type === "bearer")

  const save = useMutation({
    mutationFn: async () => {
      const body: MemberBackendWrite = {
        title: title.trim() || null,
        url: url.trim(),
        auth: {
          type: authType,
          ...(authType === "api_key" ? { header_name: headerName.trim() } : {}),
          secret: needsSecret && secretProvided
            ? secretMode === "value"
              ? { value: secret }
              : { key_vault_secret_id: keyVaultId.trim(), key_vault_identity_client_id: keyVaultIdentity.trim() || null }
            : null,
        },
        // Keep the member's rule when editing; the pool sets it on publish either way.
        circuit_breaker: creating ? breaker ?? DEFAULT_BREAKER : backend.circuit_breaker ?? breaker ?? DEFAULT_BREAKER,
        etag: creating ? null : (await api.backend(backend.id)).etag,
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
    && (!needsSecret || secretProvided || canKeepSecret)

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
      <div className="span-2"><Field label={t.url}><input value={url} placeholder="https://my-endpoint.example.com/openai" onChange={(event) => setUrl(event.target.value)} /></Field></div>
      <Field label={t.auth}>
        <select value={authType} onChange={(event) => setAuthType(event.target.value as AuthType)}>
          <option value="api_key">{t.authApiKey}</option>
          <option value="bearer">{t.authBearer}</option>
          <option value="managed_identity">{t.authManagedIdentity}</option>
          <option value="none">{t.authNone}</option>
        </select>
      </Field>
      {authType === "api_key" ? <Field label={t.headerName}><input value={headerName} onChange={(event) => setHeaderName(event.target.value)} /></Field> : <span />}
      {authType === "managed_identity" && <div className="state span-2">{t.miResourceHint}</div>}
      {needsSecret && <>
        <Field label={t.secretSource}>
          <select value={secretMode} onChange={(event) => setSecretMode(event.target.value as "value" | "key_vault")}>
            <option value="value">{t.secretValue}</option>
            <option value="key_vault">{t.secretKeyVault}</option>
          </select>
        </Field>
        {secretMode === "value"
          ? <Field label={t.secretValue} hint={canKeepSecret ? t.keepSecret : t.secretValueHint}><input type="password" autoComplete="off" value={secret} onChange={(event) => setSecret(event.target.value)} /></Field>
          : <Field label={t.keyVaultSecretId} hint={canKeepSecret ? t.keepSecret : undefined}><input value={keyVaultId} placeholder="https://my-vault.vault.azure.net/secrets/endpoint-key" onChange={(event) => setKeyVaultId(event.target.value)} /></Field>}
        {secretMode === "key_vault" && <div className="span-2"><Field label={t.keyVaultIdentity}><input value={keyVaultIdentity} onChange={(event) => setKeyVaultIdentity(event.target.value)} /></Field></div>}
      </>}
      <div className="state span-2">{t.memberBreakerFromPool}</div>
    </div>
    <ErrorText error={save.error} />
  </Dialog>
}
