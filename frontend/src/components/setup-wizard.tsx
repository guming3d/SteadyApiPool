// First-run setup: checks the Azure access the user, the app and APIM's identity need, fixes
// what the user's own rights allow, and turns the rest into a ready-to-send admin request.
import { Check, CheckCircle2, CircleAlert, CircleDashed, Clock, Copy, ExternalLink, KeyRound, LoaderCircle, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react"
import { createContext, useContext, useEffect, useState, type ReactNode } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { fill, useI18n } from "../i18n"
import { attentionCount, canAdvance, elapsedMinutes, firstStep, grantable, reachable, resourceName, STEPS, stepChecks, stepState, type StepId, type StepState } from "../setup-steps"
import type { AdminRequest, DeviceCodePrompt, SetupCheck, SetupReport, SignInSource, TargetView } from "../types"
import { isPublic, signIn as publicSignIn } from "../session"
import { ApimPicker } from "./apim-picker"
import { Button, Dialog, ErrorText } from "./ui"

type Messages = ReturnType<typeof useI18n>["t"]

const SetupContext = createContext<() => void>(() => {})
/** Opens the setup wizard from anywhere, e.g. an inline "APIM can't call this account" notice. */
export const useOpenSetup = () => useContext(SetupContext)
export const SetupProvider = ({ open, children }: { open: () => void; children: ReactNode }) =>
  <SetupContext value={open}>{children}</SetupContext>

const STEP_LABEL: Record<StepId, "setupStepSignIn" | "setupStepApim" | "setupStepAccess" | "setupStepFoundry"> = {
  sign_in: "setupStepSignIn",
  apim: "setupStepApim",
  access: "setupStepAccess",
  foundry: "setupStepFoundry",
}

function checkText(check: SetupCheck, report: SetupReport, t: Messages, apim: string): { title: string; hint?: string } {
  const values = { apim, subject: check.kind === "foundry_access" ? resourceName(check.subject) : check.subject ?? "", name: report.user?.name ?? check.detail ?? "", state: check.detail ?? "" }
  const text = (title: string, hint?: string) => ({ title: fill(title, values), hint })
  const ok = check.status === "ok"
  switch (check.kind) {
    case "sign_in":
      if (ok) return text(t.chkSignedIn)
      return text(t.chkSignIn, check.detail?.startsWith("signed in to tenant") ? t.chkSignInTenant : t.chkSignInHint)
    case "apim_target":
      if (ok) return text(t.chkTargetOk)
      if (check.status === "pending") return text(t.chkTargetPending, t.chkTargetPendingHint)
      if (check.detail === "Failed") return text(t.chkTargetFailed, t.chkTargetFailedHint)
      if (check.status === "blocked") return text(t.chkTargetMissing, t.chkTargetMissingHint)
      if (check.status === "unknown") return text(t.chkTargetUnknown)
      return text(t.chkTarget, check.fix ? t.chkTargetHint : t.chkTargetFixedHint)
    case "azure_token":
      if (ok) return text(t.chkTokenOk)
      if (check.fix === "refresh_token") return text(t.chkTokenRefresh)
      if (check.fix === "consent") return text(t.chkTokenConsent, t.chkTokenConsentHint)
      return text(t.chkTokenOff, t.chkTokenOffHint)
    case "apim_access":
      if (ok) return text(check.granted_by === "owners" ? t.chkApimOwners : check.granted_by === "azure_rbac" ? t.chkApimRbac : t.chkApimOk)
      if (check.status === "blocked") return text(t.chkApimBlocked, t.chkApimBlockedHint)
      if (check.status === "unknown") return text(t.chkApimUnknown)
      return text(t.chkApimReadOnly, report.mode === "deployed" ? t.chkApimReadOnlyDeployedHint : t.chkApimReadOnlyHint)
    case "discovery":
      return ok ? text(t.chkDiscoveryOk) : text(t.chkDiscovery, t.chkDiscoveryHint)
    case "app_apim":
      if (ok) return text(t.chkAppApimOk)
      return text(check.status === "blocked" ? t.chkAppApimBlocked : t.chkAppApim, t.chkAppHint)
    case "app_discovery":
      return ok ? text(t.chkAppDiscoveryOk) : text(t.chkAppDiscovery, t.chkAppHint)
    case "apim_identity":
      if (ok) return text(t.chkIdentityOk)
      if (check.status === "pending") return text(t.chkIdentityPending, t.chkIdentityPendingHint)
      if (check.status === "unknown") return text(t.chkIdentityUnknown)
      return text(t.chkIdentity, t.chkIdentityHint)
    case "foundry_access":
      if (ok) return text(t.chkFoundryOk)
      if (check.status === "skipped") return text(t.chkFoundrySkipped)
      if (check.status === "unknown") return text(t.chkFoundryUnknown, t.chkFoundryHint)
      return text(t.chkFoundry, t.chkFoundryHint)
  }
}

function StatusIcon({ status, size = 16 }: { status: SetupCheck["status"] | StepState; size?: number }) {
  if (status === "ok") return <CheckCircle2 size={size} />
  if (status === "pending") return <Clock size={size} />
  if (status === "action" || status === "blocked" || status === "attention") return <CircleAlert size={size} />
  return <CircleDashed size={size} />
}

function CopyText({ text, label, children }: { text: string; label: string; children?: ReactNode }) {
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
function useSetupActions() {
  const queryClient = useQueryClient()
  const recheck = async () => {
    queryClient.setQueryData(keys.setup, await api.setup(true))
    void queryClient.invalidateQueries({ queryKey: keys.capabilities })
    void queryClient.invalidateQueries({ queryKey: keys.me })
    void queryClient.invalidateQueries({ queryKey: ["setup", "foundry"] })
  }
  // A different account sees different pools and deployments.
  const switched = async () => { await recheck(); void queryClient.invalidateQueries() }
  const signIn = useMutation({ mutationFn: () => api.signIn(), onSuccess: switched })
  // Starting returns the code at once; DeviceCodePanel follows the sign-in to the end.
  const device = useMutation({ mutationFn: () => api.startDeviceCode() })
  const signOut = useMutation({ mutationFn: api.signOut, onSuccess: switched })
  const enableIdentity = useMutation({ mutationFn: api.enableApimIdentity, onSuccess: recheck })
  const grant = useMutation({ mutationFn: api.grantFoundryAccess, onSuccess: recheck })
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
  return { signIn, device, signOut, switched, enableIdentity, grant, refresh, consent, renew }
}

function FixButton({ check, actions }: { check: SetupCheck; actions: ReturnType<typeof useSetupActions> }) {
  const { t } = useI18n()
  const spinner = <LoaderCircle className="spin" size={13} />
  switch (check.fix) {
    case "sign_in":
      // The public app signs the browser in again, into the APIM subscription's tenant if named.
      if (isPublic()) {
        return <Button variant="primary" onClick={() => void publicSignIn({ tenantId: check.subject })}>
          <ExternalLink size={13} />{check.subject ? t.setupSignInTenant : t.setupSignInAgain}
        </Button>
      }
      return <span className="setup-fix-group">
        <Button variant="primary" disabled={actions.signIn.isPending} onClick={() => actions.signIn.mutate()}>
          {actions.signIn.isPending ? spinner : <ExternalLink size={13} />}{t.setupSignInBrowser}
        </Button>
        {/* For machines without a local browser: SSH, Linux servers, WSL, containers. */}
        <Button disabled={actions.device.isPending} onClick={() => actions.device.mutate()}>
          {actions.device.isPending ? spinner : <KeyRound size={13} />}{t.setupSignInCode}
        </Button>
      </span>
    case "consent":
      return <Button variant="primary" onClick={actions.consent}><ShieldCheck size={13} />{t.setupConsent}</Button>
    case "refresh_token":
      return <Button variant="primary" disabled={actions.renew.isPending} onClick={() => actions.renew.mutate()}>
        {actions.renew.isPending ? spinner : <RefreshCw size={13} />}{t.setupRenew}
      </Button>
    case "enable_apim_identity":
      return <Button variant="primary" disabled={actions.enableIdentity.isPending} onClick={() => actions.enableIdentity.mutate()}>
        {actions.enableIdentity.isPending ? spinner : <ShieldCheck size={13} />}{t.setupEnableIdentity}
      </Button>
    case "grant_foundry_access":
      return <Button variant="primary" disabled={actions.grant.isPending} onClick={() => actions.grant.mutate([check.subject!])}>
        {actions.grant.isPending && actions.grant.variables?.includes(check.subject!) ? spinner : <KeyRound size={13} />}{t.setupGrant}
      </Button>
    default:
      return null
  }
}

function CheckRow({ check, report, apim, actions }: { check: SetupCheck; report: SetupReport; apim: string; actions: ReturnType<typeof useSetupActions> }) {
  const { t } = useI18n()
  const { title, hint } = checkText(check, report, t, apim)
  const attention = check.status === "action" || check.status === "blocked"
  return <li className="setup-check" data-status={check.status}>
    <span className="setup-check-icon"><StatusIcon status={check.status} /></span>
    <div className="setup-check-body">
      <div className="setup-check-head">
        <b title={check.subject ?? undefined}>{title}</b>
        {attention && <FixButton check={check} actions={actions} />}
      </div>
      {check.status !== "ok" && check.status !== "skipped" && hint && <p>{hint}</p>}
      {check.kind === "apim_target" && check.status === "pending" && <ProvisioningProgress startedAt={check.started_at} />}
      {attention && !check.fix && check.request && <p className="muted">{t.setupNoFix}</p>}
      {check.fix === "sign_in" && actions.signIn.isPending && <p className="setup-waiting"><LoaderCircle className="spin" size={13} />{t.setupSignInWaiting}</p>}
      {check.kind === "sign_in" && actions.device.data && <DeviceCodePanel key={actions.device.data.user_code} prompt={actions.device.data} onDone={() => { actions.device.reset(); return actions.switched() }} />}
      {check.kind === "sign_in" && report.local_sign_in && <SignInDetails report={report} actions={actions} />}
      {attention && check.missing.length > 0 && <details className="setup-missing">
        <summary>{t.setupMissing} ({check.missing.length})</summary>
        <ul>{check.missing.map((action) => <li key={action}><code>{action}</code></li>)}</ul>
      </details>}
      {check.request && check.status !== "ok" && <AdminRequestPanel request={check.request} open={attention && !check.fix} />}
    </div>
  </li>
}

/** Live proof that a long APIM create is being followed: elapsed time and the last poll. The
 *  setup query refetches every 5 s while a check is pending, and each poll re-renders this. */
function ProvisioningProgress({ startedAt }: { startedAt: string | null }) {
  const { t, locale } = useI18n()
  const { dataUpdatedAt } = useQuery(queries.setup())
  const minutes = elapsedMinutes(startedAt, Date.now())
  const elapsed = minutes === null ? null
    : minutes < 1 ? t.durationUnderMinute
    : minutes < 60 ? fill(t.durationMinutes, { minutes })
    : fill(t.durationHours, { hours: Math.floor(minutes / 60), minutes: minutes % 60 })
  const checked = new Date(dataUpdatedAt).toLocaleTimeString(locale === "zh" ? "zh-CN" : "en")
  return <p className="setup-waiting">
    <LoaderCircle className="spin" size={13} />
    {elapsed && <>{fill(t.setupProvisioningElapsed, { elapsed })} · </>}
    {fill(t.setupProvisioningChecked, { time: checked })}
  </p>
}

const SOURCE_LABEL: Record<SignInSource, "srcBrowser" | "srcDeviceCode" | "srcAzureCli" | "srcAzd" | "srcPowershell"> = {
  browser: "srcBrowser",
  device_code: "srcDeviceCode",
  azure_cli: "srcAzureCli",
  azd: "srcAzd",
  powershell: "srcPowershell",
}

/** Which sign-in the app is using, whether a restart keeps it, and a way to sign out of it. */
function SignInDetails({ report, actions }: { report: SetupReport; actions: ReturnType<typeof useSetupActions> }) {
  const { t } = useI18n()
  const local = report.local_sign_in!
  const appSignIn = local.source === "browser" || local.source === "device_code"
  return <div className="setup-sign-in">
    {local.source && <p>
      {fill(t.signedInVia, { source: t[SOURCE_LABEL[local.source]] })}
      {appSignIn && <> · {local.remembered ? t.signInRemembered : t.signInSessionOnly}</>}
    </p>}
    {!local.persistent && <p className="muted">{t.signInNoKeyring}</p>}
    {appSignIn && <div><Button variant="ghost" disabled={actions.signOut.isPending} onClick={() => actions.signOut.mutate()}>
      {actions.signOut.isPending && <LoaderCircle className="spin" size={13} />}{t.signOut}
    </Button></div>}
    <ErrorText error={actions.device.error ?? actions.signOut.error} />
  </div>
}

/** The device code to enter elsewhere, followed until the sign-in finishes or fails. */
function DeviceCodePanel({ prompt, onDone }: { prompt: DeviceCodePrompt; onDone: () => Promise<void> }) {
  const { t } = useI18n()
  const status = useQuery({
    queryKey: ["setup", "device-code", prompt.user_code],
    queryFn: api.deviceCodeStatus,
    refetchInterval: (query) => query.state.data?.state === "pending" || !query.state.data ? 2_000 : false,
  })
  const state = status.data?.state
  useEffect(() => {
    if (state === "done") void onDone()
  }, [state])
  if (state === "done") return null
  return <div className="device-code">
    <p>{fill(t.deviceCodeOpen, { uri: prompt.verification_uri })}</p>
    <div className="device-code-row">
      <code className="device-code-value">{prompt.user_code}</code>
      <CopyText text={prompt.user_code} label={t.deviceCodeLabel} />
      <a href={prompt.verification_uri} target="_blank" rel="noreferrer"><ExternalLink size={13} />{prompt.verification_uri}</a>
    </div>
    {state === "failed"
      ? <ErrorText error={fill(t.deviceCodeFailed, { error: status.data?.error ?? "" })} />
      : <p className="setup-waiting"><LoaderCircle className="spin" size={13} />{fill(t.deviceCodeWaiting, { time: new Date(prompt.expires_on * 1000).toLocaleTimeString() })}</p>}
  </div>
}

export function SetupWizard({ open = true, page = false, onClose }: { open?: boolean; page?: boolean; onClose?: () => void }) {
  const { t } = useI18n()
  const setup = useQuery(queries.setup())
  const capabilities = useQuery({ ...queries.capabilities(), enabled: setup.data?.can_operate === true })
  const target = useQuery({ ...queries.target(), enabled: setup.data?.mode === "local" || setup.data?.mode === "public" || setup.data?.mode === "guest" })
  const actions = useSetupActions()
  const [chosen, setChosen] = useState<StepId | null>(null)
  const report = setup.data
  const step = chosen ?? firstStep(report)
  const index = STEPS.indexOf(step)
  const apim = capabilities.data?.apim_name ?? target.data?.apim_name ?? "APIM"
  const checks = stepChecks(report, step)
  const count = attentionCount(report)
  const toGrant = grantable(checks)
  const advance = canAdvance(report, step)
  const error = actions.signIn.error ?? actions.enableIdentity.error ?? actions.grant.error ?? actions.refresh.error ?? actions.renew.error

  const body = !report
    ? setup.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div> : <ErrorText error={setup.error} />
    : <div className="setup">
      {page && !report.can_operate && <div className="callout warning"><ShieldAlert size={15} /><div><span>{t.setupBlocked}</span></div></div>}
      <ol className="setup-steps">
        {STEPS.map((candidate, position) => {
          const state = stepState(report, candidate)
          return <li key={candidate}>
            <button type="button" className={candidate === step ? "active" : ""} data-state={state} aria-current={candidate === step ? "step" : undefined} disabled={!reachable(report, candidate, step)} onClick={() => setChosen(candidate)}>
              <span className="setup-step-number">{state === "ok" ? <Check size={13} /> : position + 1}</span>
              {t[STEP_LABEL[candidate]]}
            </button>
          </li>
        })}
      </ol>
      {stepState(report, step) === "waiting"
        ? <div className="state">{t.setupStepWaiting}</div>
        : !checks.length
        ? <div className="state"><CheckCircle2 size={14} />{t.setupNoFoundry}</div>
        : <ul className="setup-checks">{checks.map((check) => <CheckRow key={check.id} check={check} report={report} apim={apim} actions={actions} />)}</ul>}
      {step === "apim" && target.data && <TargetPanel target={target.data} report={report} />}
      {toGrant.length > 1 && <div className="setup-bulk">
        <Button disabled={actions.grant.isPending} onClick={() => actions.grant.mutate(toGrant)}><KeyRound size={13} />{fill(t.setupGrantAll, { count: toGrant.length })}</Button>
      </div>}
      {actions.grant.isSuccess && <div className="state"><Clock size={14} />{t.setupPropagation}</div>}
      <ErrorText error={error} />
    </div>

  const footer = <>
    <span className="setup-summary" data-ready={report?.ready} data-waiting={!!report && !report.ready && count === 0 && !advance}>
      {report && (report.ready ? <><CheckCircle2 size={14} />{t.setupAllSet}</>
        : count > 0 ? <><CircleAlert size={14} />{fill(t.setupAttention, { count })}</>
        // Nothing to fix, yet Next is off: the step is still running (APIM provisioning).
        : !advance && index < STEPS.length - 1 && <><Clock size={14} />{t.setupNextPending}</>)}
    </span>
    <Button variant="ghost" disabled={actions.refresh.isPending} onClick={() => actions.refresh.mutate()}>
      <RefreshCw size={13} className={actions.refresh.isPending ? "spin" : ""} />{t.setupCheckAgain}
    </Button>
    {index > 0 && <Button onClick={() => setChosen(STEPS[index - 1]!)}>{t.setupBack}</Button>}
    {index < STEPS.length - 1
      ? <Button variant="primary" disabled={!advance} onClick={() => setChosen(STEPS[index + 1]!)}>{t.setupNext}</Button>
      : !page && <Button variant="primary" onClick={onClose}>{t.setupDone}</Button>}
  </>

  if (page) {
    return <section className="card setup-page">
      <header className="dialog-header"><div><h2>{t.setupWizardTitle}</h2><p>{t.setupWizardHint}</p></div></header>
      <div className="dialog-body">{body}</div>
      <footer className="dialog-footer">{footer}</footer>
    </section>
  }
  return <Dialog open={open} wide onClose={() => onClose?.()} title={t.setupWizardTitle} description={t.setupWizardHint} footer={footer}>
    {body}
  </Dialog>
}

/** Where the APIM service comes from, and the picker when it can be changed here. */
function TargetPanel({ target, report }: { target: TargetView; report: SetupReport }) {
  const { t } = useI18n()
  const check = report.checks.find((item) => item.kind === "apim_target")
  // Open straight away when there is nothing to manage yet.
  const [open, setOpen] = useState(check?.status === "action" || check?.status === "blocked")
  if (!target.can_change) {
    if (target.source === "guest") return <p className="muted">{t.targetGuest}</p>
    return target.source === "environment" ? <p className="muted">{t.targetFixed}</p> : null
  }
  return <div className="setup-target">
    {target.source === "saved" && <p className="muted">{t.targetSaved}</p>}
    {target.source === "browser" && <p className="muted">{t.targetBrowser}</p>}
    {open
      ? <ApimPicker target={target} user={report.user} />
      : <div><Button onClick={() => setOpen(true)}>{t.targetChange}</Button></div>}
  </div>
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

/** Inline status of APIM's access to some Foundry accounts, with a one-click grant. */
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
