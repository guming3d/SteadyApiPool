// First-run setup: picks or creates the gateway (an APIM service or a LiteLLM gateway), checks
// the Azure access the user, the app and the gateway's identity need, fixes what the user's own
// rights allow, and turns the rest into a ready-to-send admin request.
import { Check, CheckCircle2, CircleAlert, CircleDashed, Clock, ExternalLink, KeyRound, LoaderCircle, Network, RefreshCw, Server, ShieldAlert, ShieldCheck } from "lucide-react"
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "../api"
import { api as apimApi } from "../apim/api"
import { fill, useI18n, type Messages } from "../i18n"
import { PRODUCTS } from "../products"
import { attentionCount, canAdvance, elapsedMinutes, firstStep, grantable, reachable, resourceName, STEPS, stepChecks, stepState, type StepId, type StepState } from "../setup-steps"
import type { DeviceCodePrompt, Product, SetupCheck, SetupReport, SignInSource, TargetView } from "../types"
import { isPublic, signIn as publicSignIn } from "../session"
import { AdminRequestPanel, CopyText, useSetupActions, useSetupI18n } from "./setup"
import { Button, Dialog, ErrorText } from "./ui"

export { SetupButton, SetupProvider } from "./setup"

const STEP_LABEL: Record<StepId, "setupStepSignIn" | "setupStepGateway" | "setupStepAccess" | "setupStepFoundry"> = {
  sign_in: "setupStepSignIn",
  gateway: "setupStepGateway",
  access: "setupStepAccess",
  foundry: "setupStepFoundry",
}

function checkText(check: SetupCheck, report: SetupReport, t: Messages, gateway: string): { title: string; hint?: string } {
  const values = { gateway, subject: check.kind === "foundry_access" ? resourceName(check.subject) : check.subject ?? "", name: report.user?.name ?? check.detail ?? "", state: check.detail ?? "" }
  const text = (title: string, hint?: string) => ({ title: fill(title, values), hint })
  const ok = check.status === "ok"
  switch (check.kind) {
    case "sign_in":
      if (ok) return text(t.chkSignedIn)
      return text(t.chkSignIn, check.detail?.startsWith("signed in to tenant") ? t.chkSignInTenant : t.chkSignInHint)
    case "gateway_target":
      if (ok) return text(t.chkTargetOk)
      if (check.status === "pending") return text(t.chkTargetPending, check.detail === "DeployingAks" ? t.chkTargetPendingAksHint : t.chkTargetPendingHint)
      if (check.detail === "Failed" || check.detail === "DeployFailed") return text(t.chkTargetFailed, t.chkTargetFailedHint)
      if (check.status === "blocked") return text(t.chkTargetMissing, t.chkTargetMissingHint)
      if (check.status === "unknown") return text(t.chkTargetUnknown)
      return text(t.chkTarget, check.fix ? t.chkTargetHint : t.chkTargetFixedHint)
    case "gateway_health":
      if (ok) return text(t.chkHealthOk)
      if (check.detail === "master_key_refused") return text(t.chkHealthKey, check.fix ? t.chkHealthConnectHint : t.chkHealthKeyHint)
      if (check.detail?.startsWith("database")) return text(t.chkHealthDatabase, t.chkHealthDatabaseHint)
      return text(t.chkHealth, check.fix ? t.chkHealthConnectHint : t.chkHealthHint)
    case "azure_token":
      if (ok) return text(t.chkTokenOk)
      if (check.fix === "refresh_token") return text(t.chkTokenRefresh)
      if (check.fix === "consent") return text(t.chkTokenConsent, t.chkTokenConsentHint)
      return text(t.chkTokenOff, t.chkTokenOffHint)
    case "gateway_access":
      if (ok) return text(check.granted_by === "owners" ? t.chkAccessOwners : check.granted_by === "azure_rbac" ? t.chkAccessRbac : t.chkAccessOk)
      if (check.status === "blocked") return text(t.chkAccessBlocked, t.chkAccessBlockedHint)
      if (check.status === "unknown") return text(t.chkAccessUnknown)
      // APIM deployed: the app can write; the user needs to be an owner or hold the role.
      return text(t.chkAccessReadOnly, report.product === "apim" && report.mode === "deployed" ? t.chkAccessReadOnlyDeployedHint : t.chkAccessReadOnlyHint)
    case "discovery":
      return ok ? text(t.chkDiscoveryOk) : text(t.chkDiscovery, t.chkDiscoveryHint)
    case "app_gateway":
      if (ok) return text(t.chkAppGatewayOk)
      // APIM: the app can read the service but not change it.
      return text(check.status === "blocked" ? t.chkAppGatewayBlocked : t.chkAppGateway, t.chkAppHint)
    case "app_discovery":
      return ok ? text(t.chkAppDiscoveryOk) : text(t.chkAppDiscovery, t.chkAppHint)
    case "gateway_identity":
      if (ok) return text(t.chkIdentityOk)
      if (check.status === "unknown") return text(t.chkIdentityUnknown)
      if (check.status === "pending") return text(t.chkIdentityPending, t.chkIdentityPendingHint)
      if (check.detail === "external_unknown") return text(t.chkIdentityExternal, t.chkIdentityExternalHint)
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

function FixButton({ check, actions }: { check: SetupCheck; actions: ReturnType<typeof useSetupActions> }) {
  const { t } = useSetupI18n()
  const queryClient = useQueryClient()
  // APIM only: turns on the service's system-assigned identity with the user's own rights.
  const enableIdentity = useMutation({
    mutationFn: apimApi.enableApimIdentity,
    onSuccess: async () => queryClient.setQueryData(keys.setup, await api.setup(true)),
  })
  const spinner = <LoaderCircle className="spin" size={13} />
  switch (check.fix) {
    case "sign_in":
      // The public app signs the browser in again, into the gateway subscription's tenant if named.
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
      return <span className="setup-fix-group">
        <Button variant="primary" disabled={enableIdentity.isPending} onClick={() => enableIdentity.mutate()}>
          {enableIdentity.isPending ? spinner : <ShieldCheck size={13} />}{t.setupEnableIdentity}
        </Button>
        <ErrorText error={enableIdentity.error} />
      </span>
    case "grant_foundry_access":
      return <Button variant="primary" disabled={actions.grant.isPending} onClick={() => actions.grant.mutate([check.subject!])}>
        {actions.grant.isPending && actions.grant.variables?.includes(check.subject!) ? spinner : <KeyRound size={13} />}{t.setupGrant}
      </Button>
    default:
      return null
  }
}

function CheckRow({ check, report, gateway, actions }: { check: SetupCheck; report: SetupReport; gateway: string; actions: ReturnType<typeof useSetupActions> }) {
  const { t } = useSetupI18n()
  const { title, hint } = checkText(check, report, t, gateway)
  const attention = check.status === "action" || check.status === "blocked"
  return <li className="setup-check" data-status={check.status}>
    <span className="setup-check-icon"><StatusIcon status={check.status} /></span>
    <div className="setup-check-body">
      <div className="setup-check-head">
        <b title={check.subject ?? undefined}>{title}</b>
        {attention && <FixButton check={check} actions={actions} />}
      </div>
      {check.status !== "ok" && check.status !== "skipped" && hint && <p>{hint}</p>}
      {check.kind === "gateway_target" && check.status === "pending" && check.detail !== "DeployingAks" && <ProvisioningProgress startedAt={check.started_at} />}
      {check.kind === "gateway_target" && (check.detail === "Failed" || check.detail === "DeployFailed") && check.missing[0] && <p className="warn-text">{check.missing[0]}</p>}
      {attention && !check.fix && check.request && <p className="muted">{t.setupNoFix}</p>}
      {check.fix === "sign_in" && actions.signIn.isPending && <p className="setup-waiting"><LoaderCircle className="spin" size={13} />{t.setupSignInWaiting}</p>}
      {check.kind === "sign_in" && actions.device.data && <DeviceCodePanel key={actions.device.data.user_code} prompt={actions.device.data} onDone={() => { actions.device.reset(); return actions.switched() }} />}
      {check.kind === "sign_in" && report.local_sign_in && <SignInDetails report={report} actions={actions} />}
      {attention && check.missing.length > 0 && check.kind !== "gateway_target" && <details className="setup-missing">
        <summary>{t.setupMissing} ({check.missing.length})</summary>
        <ul>{check.missing.map((action) => <li key={action}><code>{action}</code></li>)}</ul>
      </details>}
      {check.request && check.status !== "ok" && <AdminRequestPanel request={check.request} open={attention && !check.fix} />}
    </div>
  </li>
}

/** Live proof that a gateway deployment is being followed: elapsed time and the last poll. The
 *  setup query refetches every 5 s while a check is pending, and each poll re-renders this. */
function ProvisioningProgress({ startedAt }: { startedAt: string | null }) {
  const { t, locale } = useSetupI18n()
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
  const { t } = useSetupI18n()
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
  const { t } = useSetupI18n()
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
  const { t } = useSetupI18n()
  const setup = useQuery(queries.setup())
  const target = useQuery(queries.target())
  const actions = useSetupActions()
  const [chosen, setChosen] = useState<StepId | null>(null)
  const report = setup.data
  const step = chosen ?? firstStep(report)
  const index = STEPS.indexOf(step)
  const gateway = target.data?.gateway_name ?? t.gatewayFallbackName
  const checks = stepChecks(report, step)
  const count = attentionCount(report)
  const toGrant = grantable(checks)
  const advance = canAdvance(report, step)
  const error = actions.signIn.error ?? actions.grant.error ?? actions.refresh.error ?? actions.renew.error

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
        : <ul className="setup-checks">{checks.map((check) => <CheckRow key={check.id} check={check} report={report} gateway={gateway} actions={actions} />)}</ul>}
      {step === "gateway" && target.data && <TargetPanel target={target.data} report={report} />}
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
        // Nothing to fix, yet Next is off: the step is still running (the gateway deploying).
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

/** Where the gateway comes from, and the picker when it can be changed here. */
function TargetPanel({ target, report }: { target: TargetView; report: SetupReport }) {
  const { t } = useSetupI18n()
  // Open straight away when there is nothing to manage yet, the gateway can't be reached, or
  // a new AKS gateway is being installed (the picker's progress drives that on).
  const [open, setOpen] = useState(target.provisioning || report.checks.some((item) =>
    (item.kind === "gateway_target" || item.kind === "gateway_health")
    && (item.status === "action" || item.status === "blocked")))
  // The kind of gateway to pick from: the chosen one's, else the user picks (the public app
  // manages APIM services only).
  const [product, setProduct] = useState<Product | null>(target.product ?? (report.mode === "public" ? "apim" : null))
  if (!target.can_change) {
    if (target.source === "guest") return <p className="muted">{t.targetGuest}</p>
    return target.source === "environment" ? <p className="muted">{t.targetFixed}</p> : null
  }
  const Picker = product && PRODUCTS[product].Picker
  return <div className="setup-target">
    {target.source === "saved" && <p className="muted">{t.targetSaved}</p>}
    {target.source === "browser" && <p className="muted">{t.targetBrowser}</p>}
    {!open
      ? <div><Button onClick={() => setOpen(true)}>{t.targetChange}</Button></div>
      : <>
        {report.mode !== "public" && <ProductChoice product={product} onChange={setProduct} />}
        {Picker && <Picker key={product} target={target} user={report.user} />}
      </>}
  </div>
}

/** APIM or LiteLLM: which kind of gateway the picker below lists and creates. */
function ProductChoice({ product, onChange }: { product: Product | null; onChange: (product: Product) => void }) {
  const { t } = useI18n()
  const options: Array<{ id: Product; icon: typeof Server; title: string; hint: string }> = [
    { id: "litellm", icon: Network, title: t.productLitellm, hint: t.productLitellmHint },
    { id: "apim", icon: Server, title: t.productApim, hint: t.productApimHint },
  ]
  return <div className="pool-choices" role="radiogroup" aria-label={t.productChoice}>
    {options.map(({ id, icon: Icon, title, hint }) => <label key={id} className="pool-choice" data-selected={product === id}>
      <input type="radio" name="gateway-product" checked={product === id} onChange={() => onChange(id)} />
      <span className="list-copy"><b>{title}</b><small>{hint}</small></span>
      <Icon size={16} />
    </label>)}
  </div>
}

