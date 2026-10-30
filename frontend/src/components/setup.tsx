// Setup pieces used across the app: opening the wizard, its fixes, the admin request panel, the
// top-bar button and the inline Foundry access notice. The wizard itself is setup-wizard.tsx.
import { Check, CheckCircle2, CircleAlert, Copy, ExternalLink, KeyRound, LoaderCircle, ShieldAlert, ShieldCheck } from "lucide-react"
import { createContext, useContext, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, ApiError, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import { attentionCount, grantable, resourceName } from "../setup-steps"
import type { AdminRequest, SetupCheck } from "../types"
import { isPublic, signIn as publicSignIn } from "../session"
import { Button, ErrorText } from "./ui"

const SetupContext = createContext<() => void>(() => {})
/** Opens the setup wizard from anywhere, e.g. an inline "the gateway can't call this account" notice. */
export const useOpenSetup = () => useContext(SetupContext)
export const SetupProvider = ({ open, children }: { open: () => void; children: ReactNode }) =>
  <SetupContext value={open}>{children}</SetupContext>

/** The setup report's strings, in the wording of the gateway it is about. */
export function useSetupI18n() {
  return useI18n(useQuery(queries.setup()).data?.product)
}

export function CopyText({ text, label, children }: { text: string; label: string; children?: ReactNode }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return children
    ? <Button variant="ghost" onClick={() => void copy()}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? t.copied : children}</Button>
    : <button type="button" className="icon-button" title={copied ? t.copied : t.copy} aria-label={`${t.copy} ${label}`} onClick={() => void copy()}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
}

/** Who needs what, where, plus the command and a message to send to an administrator. */
export function AdminRequestPanel({ request, open }: { request: AdminRequest; open?: boolean }) {
  const { t, locale } = useI18n()
  const message = request.role
    ? fill(t.setupMessageRole, { role: request.role, assignee: request.assignee_id ? `${request.assignee} (${request.assignee_id})` : request.assignee, scope: request.scope ?? "", command: request.command }, locale)
    : fill(t.setupMessageCommand, { command: request.command }, locale)
  return <details className="admin-request" open={open}>
    <summary><KeyRound size={13} />{t.setupAskAdmin}</summary>
    <div className="admin-request-body">
      {request.role && <dl>
        <div><dt>{t.setupAssignee}</dt><dd>{request.assignee}{request.assignee_id && <code>{request.assignee_id}</code>}</dd></div>
        <div><dt>{t.setupRole}</dt><dd>{request.role}</dd></div>
        {request.scope && <div className="wide"><dt>{t.setupScope}</dt><dd><code>{request.scope}</code></dd></div>}
      </dl>}
      <div className="code-block"><pre><code>{request.command}</code></pre><CopyText text={request.command} label="command" /></div>
      <div className="admin-request-actions">
        <CopyText text={message} label={t.setupCopyMessage}>{t.setupCopyMessage}</CopyText>
        {request.portal_url && <a href={request.portal_url} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.setupOpenIam}</a>}
      </div>
    </div>
  </details>
}

/** Runs the fixes and refreshes every query that depends on Azure access afterwards. */
export function useSetupActions() {
  const queryClient = useQueryClient()
  const recheck = async () => {
    queryClient.setQueryData(keys.setup, await api.setup(true))
    // Each product's capabilities ([product, "capabilities"]).
    void queryClient.invalidateQueries({ predicate: (query) => query.queryKey[1] === "capabilities" })
    void queryClient.invalidateQueries({ queryKey: keys.me })
    void queryClient.invalidateQueries({ queryKey: ["setup", "foundry"] })
  }
  // A different account sees different pools and deployments.
  const switched = async () => { await recheck(); void queryClient.invalidateQueries() }
  const signIn = useMutation({ mutationFn: () => api.signIn(), onSuccess: switched })
  // Starting returns the code at once; DeviceCodePanel follows the sign-in to the end.
  const device = useMutation({ mutationFn: () => api.startDeviceCode() })
  const signOut = useMutation({ mutationFn: api.signOut, onSuccess: switched })
  const grant = useMutation({
    mutationFn: api.grantFoundryAccess,
    onSuccess: async (checks) => {
      // The grant's own answer counts at once: Azure lists a new assignment only a while later.
      const fresh = new Map(checks.map((check) => [check.subject, check]))
      queryClient.setQueriesData<SetupCheck[]>({ queryKey: ["setup", "foundry"] }, (old) =>
        old?.map((check) => (check.subject && fresh.get(check.subject)) || check))
      await recheck()
    },
  })
  const refresh = useMutation({ mutationFn: recheck })
  const consent = () => {
    // The public app asks Entra for consent again; it returns to this page.
    if (isPublic()) return void publicSignIn({ consent: true })
    // Easy Auth signs in again and asks for the Azure scope; it returns to this page.
    window.location.assign(`/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(window.location.pathname + window.location.search)}`)
  }
  const renew = useMutation({
    mutationFn: async () => {
      const response = await fetch("/.auth/refresh", { credentials: "same-origin" })
      if (!response.ok) return consent()
      await recheck()
    },
  })
  return { signIn, device, signOut, switched, grant, refresh, consent, renew }
}

/** Top-bar entry point: green when ready, amber with a count when something needs attention. */
export function SetupButton({ onOpen }: { onOpen: () => void }) {
  const { t } = useI18n()
  const setup = useQuery(queries.setup())
  if (!setup.data) return null
  const count = attentionCount(setup.data)
  return <button type="button" className="setup-button" data-ready={setup.data.ready} data-tour="setup" title={t.setupOpen} onClick={onOpen}>
    {setup.data.ready ? <ShieldCheck size={14} /> : <ShieldAlert size={14} />}{t.setupTitle}
    {count > 0 && <span className="setup-count">{count}</span>}
  </button>
}

/** Inline status of the gateway's access to some Foundry accounts, with a one-click grant. */
export function FoundryAccessNotice({ accountIds }: { accountIds: string[] }) {
  const { t, locale } = useI18n()
  const openSetup = useOpenSetup()
  const actions = useSetupActions()
  const ids = [...new Set(accountIds)].sort()
  const access = useQuery(queries.foundryAccess(ids))
  if (!ids.length) return null
  if (access.isLoading) return <div className="state"><LoaderCircle className="spin" size={14} />{t.foundryAccessChecking}</div>
  if (access.error) return <ErrorText error={access.error} />
  const checks = access.data ?? []
  if (checks.some((check) => check.status === "skipped")) {
    return <div className="foundry-access" data-tone="warn">
      <CircleAlert size={14} /><span>{t.foundryNoIdentity}</span>
      <button type="button" className="link-button" onClick={openSetup}>{t.setupOpen}</button>
    </div>
  }
  const missing = checks.filter((check) => check.status !== "ok")
  if (!missing.length) {
    return <div className="foundry-access" data-tone="ok"><CheckCircle2 size={14} /><span>{fill(t.foundryAccessAll, { count: checks.length })}</span></div>
  }
  const toGrant = grantable(missing)
  const commands = missing.flatMap((check) => check.request ? [check.request.command] : []).join("\n")
  return <div className="foundry-access" data-tone="warn">
    <CircleAlert size={14} />
    <span>{fill(t.foundryAccessMissing, { names: missing.map((check) => resourceName(check.subject)) }, locale)}</span>
    {toGrant.length > 0 && <Button variant="primary" disabled={actions.grant.isPending} onClick={() => actions.grant.mutate(toGrant)}>
      {actions.grant.isPending ? <LoaderCircle className="spin" size={13} /> : <KeyRound size={13} />}
      {toGrant.length > 1 ? fill(t.setupGrantAll, { count: toGrant.length }) : t.setupGrant}
    </Button>}
    {toGrant.length < missing.length && commands && <details className="foundry-access-admin">
      <summary>{t.setupAskAdmin}</summary>
      <div className="code-block"><pre><code>{commands}</code></pre><CopyText text={commands} label="az role assignment" /></div>
    </details>}
    <ErrorText error={actions.grant.error} />
  </div>
}

/** A gateway the check before switching refused (424): why, and a way to choose it anyway and
 *  fix it in setup; the current gateway stays until then. Any other error is shown as it is. */
export function SwitchRefused({ error, name, busy, onForce }: {
  error: unknown
  name: string | undefined
  busy: boolean
  onForce: () => void
}) {
  const { t } = useI18n()
  if (!(error instanceof ApiError && error.status === 424)) return <ErrorText error={error} />
  return <div className="callout warning" role="alert">
    <CircleAlert size={15} />
    <div>
      <b>{fill(t.switchRefused, { name: name ?? "" })}</b>
      <span>{error.message}</span>
      <small className="muted">{t.switchRefusedHint}</small>
    </div>
    <Button disabled={busy} onClick={onForce}>{busy && <LoaderCircle className="spin" size={13} />}{t.switchAnyway}</Button>
  </div>
}
