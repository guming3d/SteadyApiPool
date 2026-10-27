// Choose the LiteLLM gateway to manage, deploy one on Container Apps, or connect one that runs
// elsewhere (e.g. on AKS, with the files this app generates). Local only; the public app is not
// available with the gateway yet.
import { CheckCircle2, CircleAlert, Cloud, Download, ExternalLink, Link2, LoaderCircle, Plug, Plus, RefreshCw, Server, Trash2, Unplug } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, ApiError, keys, queries } from "../api"
import { formatMoney } from "../cost"
import {
  AKS_PROFILE_ESTIMATE,
  DEFAULT_ANSWERS,
  isGatewayName,
  PROFILE_ESTIMATE,
  recommend,
  suggestGatewayName,
  type ChoiceAnswers,
  type Platform,
} from "../gateway-choice"
import { fill, useI18n } from "../i18n"
import { targetFromServiceId, targetFromView } from "../public-target"
import { chooseTarget, isPublic, signIn as publicSignIn } from "../session"
import type { AksBundle, GatewayPlatform, GatewayProfile, GatewayView, SetupIdentity, TargetView } from "../types"
import { CopyButton } from "./call-guide"
import { Button, Dialog, ErrorText, Field } from "./ui"

const RESOURCE_GROUP = /^[-\w.()]{0,89}[-\w()]$/
const PREFERRED_LOCATIONS = ["eastus2", "swedencentral", "eastus", "westeurope", "koreacentral"]
const GUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/
const APP_INSIGHTS_ID = /^\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.Insights\/components\/[^/]+$/i

/** Everything shown depends on the gateway, so reload it all after a switch. */
function useSwitched() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries()
}

type Mode = "list" | "deploy" | "connect"

export function GatewayPicker({ target, user }: { target: TargetView; user: SetupIdentity | null }) {
  const { t } = useI18n()
  const tenants = useQuery(queries.tenants())
  const [tenantId, setTenantId] = useState<string | null>(target.tenant_id ?? user?.tenant_id ?? null)
  const [subscriptionId, setSubscriptionId] = useState(target.subscription_id ?? "")
  const [mode, setMode] = useState<Mode>("list")
  const [connectName, setConnectName] = useState<string | undefined>()
  const [deleting, setDeleting] = useState<Deletable | null>(null)
  const subscriptions = useQuery(queries.subscriptions(tenantId))
  const gateways = useQuery(queries.gateways(tenantId, subscriptionId))
  const switched = useSwitched()
  const signIn = useMutation({
    mutationFn: (tenant: string | null) => isPublic() ? publicSignIn({ tenantId: tenant }) : api.signIn(tenant ?? undefined),
    onSuccess: switched,
  })
  const select = useMutation({
    mutationFn: (gateway: GatewayView) => api.selectTarget({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: gateway.resource_group,
      gateway_name: gateway.name,
    }),
    onSuccess: (view) => { chooseTarget(targetFromView(view)); return switched() },
  })
  const disconnect = useMutation({ mutationFn: api.disconnectGateway, onSuccess: switched })

  useEffect(() => {
    // Start in the first tenant when the signed-in one is not known.
    if (!tenantId && tenants.data?.length) setTenantId(tenants.data[0]!.tenant_id)
  }, [tenantId, tenants.data])
  useEffect(() => {
    const enabled = subscriptions.data?.filter((item) => item.state === "Enabled") ?? []
    if (enabled.length && !enabled.some((item) => item.subscription_id === subscriptionId)) {
      setSubscriptionId(enabled[0]!.subscription_id)
    }
  }, [subscriptions.data, subscriptionId])

  const needsSignIn = subscriptions.error instanceof ApiError && subscriptions.error.status === 401
  const external = target.gateway_kind === "external"
  // The current gateway, when it isn't in the list: its deployment may have failed before the
  // Container App existed, and what it did create can be cleaned up from here.
  // A new AKS gateway the app is still installing: its progress drives the install on.
  const provisioning = target.provisioning && Boolean(target.gateway_name && target.resource_group)
    && target.subscription_id === subscriptionId
  const failedHere = !external && Boolean(target.gateway_name && target.resource_group)
    && target.subscription_id === subscriptionId && Boolean(gateways.data)
    && !gateways.data!.some((gateway) => gateway.name.toLowerCase() === target.gateway_name!.toLowerCase()
      && gateway.resource_group.toLowerCase() === target.resource_group!.toLowerCase())
  return <div className="gateway-picker">
    <div className="form-grid">
      {(tenants.data?.length ?? 0) > 1 && <label className="field">{t.targetTenant}
        <select value={tenantId ?? ""} onChange={(event) => { setTenantId(event.target.value); setSubscriptionId(""); setMode("list") }}>
          {tenants.data!.map((tenant) => <option key={tenant.tenant_id} value={tenant.tenant_id}>
            {tenant.display_name ?? tenant.tenant_id}{tenant.default_domain ? ` (${tenant.default_domain})` : ""}
          </option>)}
        </select>
      </label>}
      <label className="field">{t.targetSubscription}
        <select value={subscriptionId} disabled={!subscriptions.data?.length} onChange={(event) => { setSubscriptionId(event.target.value); setMode("list") }}>
          {!subscriptionId && <option value="">{subscriptions.isLoading ? t.loading : t.targetNoSubscriptions}</option>}
          {subscriptions.data?.map((item) => <option key={item.subscription_id} value={item.subscription_id} disabled={item.state !== "Enabled"}>
            {item.display_name}{item.state !== "Enabled" ? ` (${item.state})` : ""}
          </option>)}
        </select>
      </label>
    </div>
    {needsSignIn
      ? <div className="callout warning"><CircleAlert size={15} /><div>
        <span>{t.targetTenantSignIn}</span>
        <div><Button variant="primary" disabled={signIn.isPending} onClick={() => signIn.mutate(tenantId)}>
          {signIn.isPending ? <LoaderCircle className="spin" size={13} /> : <ExternalLink size={13} />}{isPublic() ? t.setupSignInTenant : t.setupSignInBrowser}
        </Button></div>
      </div></div>
      : <ErrorText error={tenants.error ?? subscriptions.error} />}

    {external && mode === "list" && <ul className="gateway-list"><li>
      <Plug size={15} />
      <div>
        <b>{target.gateway_name}</b>
        <span className="muted">{target.gateway_url} · {t.targetExternal}</span>
      </div>
      <div className="gateway-actions">
        <span className="tag" data-tone="ok">{t.targetInUse}</span>
        {target.can_change && <Button variant="ghost" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}><Unplug size={13} />{t.targetDisconnect}</Button>}
      </div>
    </li></ul>}

    {subscriptionId && mode === "list" && <>
      {gateways.isLoading
        ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
        : <ErrorText error={gateways.error} />}
      {gateways.data && (gateways.data.length
        ? <ul className="gateway-list">{gateways.data.map((gateway) => {
          const current = !external && gateway.name.toLowerCase() === target.gateway_name?.toLowerCase()
            && gateway.resource_group.toLowerCase() === target.resource_group?.toLowerCase()
          return <li key={gateway.id}>
            <Server size={15} />
            <div>
              <b>{gateway.name}</b>
              <span className="muted">{gateway.resource_group} · {gateway.location}{gateway.profile && ` · ${t[gateway.profile === "dev" ? "profileDev" : "profileProduction"]}`}
                {gateway.provisioning_state && gateway.provisioning_state !== "Succeeded" && ` · ${gateway.provisioning_state}`}</span>
              <span>
                {gateway.platform === "aks" && <span className="tag">{t.targetAks}</span>}
                {gateway.managed && <span className="tag">{t.targetManaged}</span>}
                {gateway.can_manage === true && <span className="tag" data-tone="ok">{t.targetCanManage}</span>}
                {gateway.can_manage === false && <span className="tag" data-tone="warn" title={gateway.missing.join("\n")}>{t.targetCannotManage}</span>}
              </span>
            </div>
            <div className="gateway-actions">
              {/* Only gateways this app deployed can be deleted here; the backend checks too. */}
              {gateway.managed && <Button variant="ghost" className="danger" onClick={() => setDeleting(gateway)}>
                <Trash2 size={13} />{t.targetDelete}
              </Button>}
              {current
                ? <span className="tag" data-tone="ok">{t.targetInUse}</span>
                : <Button disabled={select.isPending} onClick={() => select.mutate(gateway)}>
                  {select.isPending && select.variables?.id === gateway.id && <LoaderCircle className="spin" size={13} />}{t.targetUse}
                </Button>}
            </div>
          </li>
        })}</ul>
        : !failedHere && <div className="state">{t.targetNoGateways}</div>)}
      {provisioning && <DeployProgress
        tenantId={tenantId}
        subscriptionId={subscriptionId}
        group={target.resource_group!}
        name={target.gateway_name!}
        onDone={switched}
        onCleanedUp={switched}
      />}
      {failedHere && !provisioning && <FailedDeployment
        tenantId={tenantId}
        subscriptionId={subscriptionId}
        group={target.resource_group!}
        name={target.gateway_name!}
        onCleanUp={() => setDeleting({ id: `deployment:${target.resource_group}/${target.gateway_name}`, name: target.gateway_name!, resource_group: target.resource_group! })}
      />}
      <ErrorText error={select.error ?? signIn.error ?? disconnect.error} />
      <div className="picker-actions">
        <Button variant={gateways.data?.length ? "outline" : "primary"} onClick={() => setMode("deploy")}><Plus size={13} />{t.targetCreate}</Button>
        <Button variant="ghost" onClick={() => { setConnectName(undefined); setMode("connect") }}><Link2 size={13} />{t.targetConnect}</Button>
      </div>
    </>}
    {deleting && <DeleteGatewayDialog gateway={deleting} tenantId={tenantId} subscriptionId={subscriptionId} onClose={() => setDeleting(null)} />}
    {subscriptionId && mode === "deploy" && <DeployGateway
      tenantId={tenantId}
      subscriptionId={subscriptionId}
      onConnect={(name) => { setConnectName(name); setMode("connect") }}
      onDeployed={() => setMode("list")}
      onCancel={() => setMode("list")}
    />}
    {subscriptionId && mode === "connect" && <ConnectGatewayForm
      tenantId={tenantId}
      subscriptionId={subscriptionId}
      initialName={connectName}
      onConnected={() => setMode("list")}
      onCancel={() => setMode("list")}
    />}
  </div>
}

type Deletable = Pick<GatewayView, "id" | "name" | "resource_group">

/** A gateway whose deployment failed: why, and a way to remove what it created. */
function FailedDeployment({ tenantId, subscriptionId, group, name, onCleanUp }: {
  tenantId: string | null
  subscriptionId: string
  group: string
  name: string
  onCleanUp: () => void
}) {
  const { t } = useI18n()
  const status = useQuery(queries.deployStatus(tenantId, subscriptionId, group, name))
  if (status.data?.state !== "failed") return null
  return <ul className="gateway-list"><li data-state="failed">
    <CircleAlert size={15} />
    <div>
      <b>{name}</b>
      <span className="muted">{group} · {t.deployFailed}</span>
      {status.data.error && <span className="warn-text">{status.data.error}</span>}
    </div>
    <div className="gateway-actions">
      <Button variant="ghost" className="danger" onClick={onCleanUp}><Trash2 size={13} />{t.deployCleanup}</Button>
    </div>
  </li></ul>
}

// -- choosing where the gateway runs ---------------------------------------------------------

function DeployGateway({ tenantId, subscriptionId, onConnect, onDeployed, onCancel }: {
  tenantId: string | null
  subscriptionId: string
  onConnect: (name: string) => void
  onDeployed: () => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const [answers, setAnswers] = useState<ChoiceAnswers>(DEFAULT_ANSWERS)
  // aks: a new cluster the app deploys; aks-files: the files for a cluster the user has.
  const [platform, setPlatform] = useState<Platform | "aks-files" | null>(null)
  const recommendation = recommend(answers)
  const set = (next: Partial<ChoiceAnswers>) => setAnswers((current) => ({ ...current, ...next }))
  if (platform === "aca" || platform === "aks") {
    return <CreateGatewayForm tenantId={tenantId} subscriptionId={subscriptionId} platform={platform} profile={recommendation.profile} onDeployed={onDeployed} onCancel={() => setPlatform(null)} />
  }
  if (platform === "aks-files") {
    return <AksBundleForm subscriptionId={subscriptionId} onConnect={onConnect} onCancel={() => setPlatform(null)} />
  }
  return <section className="gateway-form gateway-choice">
    <h3>{t.choiceTitle}</h3>
    <p className="muted">{t.choiceHint}</p>
    <div className="form-grid">
      <Field label={t.choiceStage}>
        <select value={answers.stage} onChange={(event) => set({ stage: event.target.value as ChoiceAnswers["stage"] })}>
          <option value="trial">{t.choiceStageTrial}</option>
          <option value="production">{t.choiceStageProduction}</option>
        </select>
      </Field>
      <Field label={t.choiceTpm} hint={fill(t.choiceRps, { rps: recommendation.rps.toFixed(recommendation.rps < 10 ? 1 : 0) })}>
        <input type="number" min={0} step={0.5} value={answers.peakTpmMillions} onChange={(event) => set({ peakTpmMillions: Number(event.target.value) || 0 })} />
      </Field>
      <Field label={t.choiceTokens}>
        <input type="number" min={1} step={1000} value={answers.tokensPerRequest} onChange={(event) => set({ tokensPerRequest: Number(event.target.value) || 1 })} />
      </Field>
      <span />
      <label className="check span-2"><input type="checkbox" checked={answers.kubernetesTeam} onChange={(event) => set({ kubernetesTeam: event.target.checked })} />{t.choiceTeam}</label>
      <label className="check span-2"><input type="checkbox" checked={answers.selfHostedModels} onChange={(event) => set({ selfHostedModels: event.target.checked })} />{t.choiceGpu}</label>
      <label className="check span-2"><input type="checkbox" checked={answers.privateNetwork} onChange={(event) => set({ privateNetwork: event.target.checked })} />{t.choicePrivate}</label>
    </div>
    <div className="choice-result">
      {(["aca", "aks"] as const).map((candidate) => {
        const recommended = recommendation.platform === candidate
        return <div key={candidate} className="choice-card" data-recommended={recommended}>
          <header>
            {candidate === "aca" ? <Cloud size={16} /> : <Server size={16} />}
            <b>{candidate === "aca" ? t.choiceAca : t.choiceAks}</b>
            {recommended && <span className="tag" data-tone="ok">{t.choiceRecommended}</span>}
          </header>
          <p className="muted">{candidate === "aca" ? t.choiceAcaBody : t.choiceAksBody}</p>
          {recommended && <ul>{recommendation.reasons.map((reason) => <li key={reason}>{fill(t[reason], { replicas: recommendation.replicas })}</li>)}</ul>}
          <div className="choice-actions">
            <Button variant={recommended ? "primary" : "outline"} onClick={() => setPlatform(candidate)}>
              {candidate === "aca" ? t.choiceDeployAca : t.choiceDeployAks}
            </Button>
            {candidate === "aks" && <Button variant="ghost" onClick={() => setPlatform("aks-files")}><Download size={13} />{t.choiceAksFiles}</Button>}
          </div>
        </div>
      })}
    </div>
    <div className="gateway-form-actions"><Button onClick={onCancel}>{t.cancel}</Button></div>
  </section>
}

// -- deploying on Container Apps --------------------------------------------------------------

function CreateGatewayForm({ tenantId, subscriptionId, platform, profile: suggested, onDeployed, onCancel }: {
  tenantId: string | null
  subscriptionId: string
  platform: GatewayPlatform
  profile: GatewayProfile
  onDeployed: () => void
  onCancel: () => void
}) {
  const { t, locale } = useI18n()
  const options = useQuery(queries.createOptions(tenantId, subscriptionId))
  const switched = useSwitched()
  const [name, setName] = useState(() => suggestGatewayName())
  const [group, setGroup] = useState("rg-ai-gateway")
  const [location, setLocation] = useState("")
  const [profile, setProfile] = useState<GatewayProfile>(suggested)
  const [started, setStarted] = useState<{ group: string; name: string } | null>(null)
  const create = useMutation({
    mutationFn: () => api.createGateway({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: group.trim(),
      location,
      name: name.trim(),
      profile,
      platform,
    }),
    onSuccess: (gateway) => {
      // The public app keeps the choice in the browser; it deploys only on Container Apps.
      if (platform === "aca") chooseTarget(targetFromServiceId(gateway.id, tenantId))
      setStarted({ group: gateway.resource_group, name: gateway.name })
    },
  })

  const existingGroup = options.data?.resource_groups.find((item) => item.name.toLowerCase() === group.trim().toLowerCase())
  const locations = options.data?.locations ?? []
  useEffect(() => {
    // Keep a valid region: the existing group's, else a common one, else the first offered.
    if (!locations.length || locations.includes(location)) return
    const preferred = [existingGroup?.location, ...PREFERRED_LOCATIONS].find((item) => item && locations.includes(item))
    setLocation(preferred ?? locations[0] ?? "")
  }, [locations, location, existingGroup])

  if (started) {
    return <DeployProgress tenantId={tenantId} subscriptionId={subscriptionId} group={started.group} name={started.name} onDone={() => { onDeployed(); return switched() }} onCleanedUp={() => setStarted(null)} />
  }
  if (options.isLoading) return <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
  if (!options.data) return <ErrorText error={options.error} />
  const valid = isGatewayName(name.trim()) && RESOURCE_GROUP.test(group.trim()) && Boolean(location)
  const aks = platform === "aks"
  const estimate = (aks ? AKS_PROFILE_ESTIMATE : PROFILE_ESTIMATE)[profile]
  const profileHint = profile === "dev"
    ? aks ? t.aksProfileDevHint : t.profileDevHint
    : aks ? t.aksProfileProductionHint : t.profileProductionHint
  return <form className="gateway-form" onSubmit={(event) => { event.preventDefault(); if (valid) create.mutate() }}>
    <h3>{aks ? t.aksCreateTitle : t.targetCreate}</h3>
    <div className="form-grid">
      <label className="field">{t.createName}
        <input value={name} onChange={(event) => setName(event.target.value)} aria-invalid={!isGatewayName(name.trim())} />
        <small>{!isGatewayName(name.trim()) ? t.createNameInvalid : aks ? fill(t.aksNameHint, { url: `https://${name.trim()}.${location || "<region>"}.cloudapp.azure.com` }) : t.createNameHint}</small>
      </label>
      <label className="field">{t.createGroup}
        <input value={group} list="gateway-create-groups" onChange={(event) => setGroup(event.target.value)} aria-invalid={!RESOURCE_GROUP.test(group.trim())} />
        <datalist id="gateway-create-groups">{options.data.resource_groups.map((item) => <option key={item.name} value={item.name} />)}</datalist>
        <small>{existingGroup ? fill(t.createGroupExisting, { location: existingGroup.location }) : t.createGroupNew}</small>
      </label>
      <label className="field">{t.createProfile}
        <select value={profile} onChange={(event) => setProfile(event.target.value as GatewayProfile)}>
          <option value="dev">{t.profileDev}</option>
          <option value="production">{t.profileProduction}</option>
        </select>
        <small>{profileHint}</small>
      </label>
      <label className="field">{t.createLocation}
        <select value={location} disabled={!locations.length} onChange={(event) => setLocation(event.target.value)}>
          {!locations.length && <option value="">{t.createNoLocations}</option>}
          {locations.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
    </div>
    <div className="state">{fill(t.createEstimate, {
      low: formatMoney(estimate.low, "USD", locale, 0),
      high: formatMoney(estimate.high, "USD", locale, 0),
    })}</div>
    {options.data.unregistered_providers.length > 0 && <div className="state">{fill(t.createRegisterProvider, { providers: options.data.unregistered_providers.join(", ") })}</div>}
    <div className="callout warning"><CircleAlert size={15} /><div><span>{aks ? t.aksBilling : t.createBilling}</span></div></div>
    <ErrorText error={create.error} />
    <div className="gateway-form-actions">
      <Button onClick={onCancel} disabled={create.isPending}>{t.back}</Button>
      <Button type="submit" variant="primary" disabled={!valid || create.isPending}>
        {create.isPending ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{t.createSubmit}
      </Button>
    </div>
  </form>
}

const AKS_PHASES = ["azure", "cluster", "certificate"] as const

/** Follows the ARM deployment, resource by resource, until the gateway is up; on AKS also the
 *  install in the cluster and the HTTPS certificate. */
function DeployProgress({ tenantId, subscriptionId, group, name, onDone, onCleanedUp }: {
  tenantId: string | null
  subscriptionId: string
  group: string
  name: string
  onDone: () => void
  onCleanedUp: () => void
}) {
  const { t } = useI18n()
  const status = useQuery(queries.deployStatus(tenantId, subscriptionId, group, name))
  const queryClient = useQueryClient()
  const [cleaning, setCleaning] = useState(false)
  const retry = useMutation({
    mutationFn: () => api.retryInstall({ tenant_id: tenantId, subscription_id: subscriptionId, resource_group: group, gateway_name: name }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.deployStatus(subscriptionId, group, name) }),
  })
  const state = status.data?.state
  const aks = status.data?.platform === "aks"
  const phase = status.data?.phase ?? ("azure" as const)
  // Once: reloading everything re-renders this, with a new onDone.
  const finished = useRef(false)
  useEffect(() => {
    if (state !== "succeeded" || finished.current) return
    finished.current = true
    onDone()
  }, [state, onDone])
  const phaseLabel = { azure: t.deployPhaseAzure, cluster: t.deployPhaseCluster, certificate: t.deployPhaseCertificate }
  return <section className="gateway-form gateway-deploy">
    <h3>{fill(t.deployTitle, { name })}</h3>
    <p className="muted">{aks ? t.deployHintAks : t.deployHint}</p>
    {aks && <ol className="deploy-phases">
      {AKS_PHASES.map((item, index) => {
        const current = AKS_PHASES.indexOf(phase === "done" ? "certificate" : phase)
        const done = phase === "done" || index < current
        const failed = state === "failed" && index === current
        return <li key={item} data-state={failed ? "failed" : done ? "succeeded" : index === current ? "running" : "pending"}>
          {failed ? <CircleAlert size={14} /> : done ? <CheckCircle2 size={14} /> : index === current ? <LoaderCircle className="spin" size={14} /> : <span className="phase-dot" />}
          <span>{phaseLabel[item]}</span>
        </li>
      })}
    </ol>}
    {aks && state === "running" && phase === "cluster" && <p className="muted">{t.deployPhaseClusterHint}</p>}
    {aks && state === "running" && phase === "certificate" && <p className="muted">{fill(t.deployPhaseCertificateHint, { url: `https://${name}.<region>.cloudapp.azure.com` })}</p>}
    {status.data && (!aks || phase === "azure") && <ul className="deploy-steps">
      {status.data.steps.map((step) => <li key={`${step.resource_type}/${step.name}`} data-state={step.state.toLowerCase()}>
        {step.state === "Succeeded" ? <CheckCircle2 size={14} /> : step.state === "Failed" ? <CircleAlert size={14} /> : <LoaderCircle className="spin" size={14} />}
        <span><b>{step.name}</b><small className="muted">{step.resource_type}</small>{step.error && <small className="warn-text">{step.error}</small>}</span>
      </li>)}
    </ul>}
    {(!status.data || state === "running" || state === "none") && <div className="state"><LoaderCircle className="spin" size={14} />{t.deployRunning}</div>}
    {state === "succeeded" && <div className="state" data-status="active"><CheckCircle2 size={14} />{t.deployDone}</div>}
    {status.data?.log_tail && <details className="deploy-log" open={state === "failed"}>
      <summary>{t.deployLog}</summary>
      <pre>{status.data.log_tail}</pre>
    </details>}
    {state === "failed" && <div className="callout warning"><CircleAlert size={15} /><div>
      <b>{t.deployFailed}</b><span>{status.data?.error}</span>
      <span className="muted">{t.deployCleanupHint}</span>
      <div className="picker-actions">
        {aks && phase === "cluster" && <Button disabled={retry.isPending} onClick={() => retry.mutate()}>
          {retry.isPending ? <LoaderCircle className="spin" size={13} /> : <RefreshCw size={13} />}{t.deployRetryInstall}
        </Button>}
        <Button variant="ghost" className="danger" onClick={() => setCleaning(true)}><Trash2 size={13} />{t.deployCleanup}</Button>
      </div>
      <ErrorText error={retry.error} />
    </div></div>}
    {cleaning && <DeleteGatewayDialog
      gateway={{ id: `deployment:${group}/${name}`, name, resource_group: group }}
      tenantId={tenantId}
      subscriptionId={subscriptionId}
      onClose={() => setCleaning(false)}
      onDeleted={onCleanedUp}
    />}
    <ErrorText error={status.error} />
  </section>
}

// -- AKS: the files to run, then connect ---------------------------------------------------

function AksBundleForm({ subscriptionId, onConnect, onCancel }: {
  subscriptionId: string
  onConnect: (name: string) => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const [cluster, setCluster] = useState("")
  const [group, setGroup] = useState("")
  const [name, setName] = useState(() => suggestGatewayName())
  const [location, setLocation] = useState("eastus2")
  const [namespace, setNamespace] = useState("litellm")
  const bundle = useMutation({
    mutationFn: () => api.aksBundle({
      subscription_id: subscriptionId,
      resource_group: group.trim(),
      cluster: cluster.trim(),
      name: name.trim(),
      location: location.trim(),
      namespace: namespace.trim(),
    }),
  })
  const valid = Boolean(cluster.trim()) && RESOURCE_GROUP.test(group.trim()) && isGatewayName(name.trim())
    && /^[a-z0-9]{2,40}$/.test(location.trim()) && /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(namespace.trim())
  return <section className="gateway-form">
    <h3>{t.aksTitle}</h3>
    <p className="muted">{t.aksHint}</p>
    <div className="form-grid">
      <Field label={t.aksCluster}><input value={cluster} onChange={(event) => setCluster(event.target.value)} placeholder="aks-prod" /></Field>
      <Field label={t.aksGroup}><input value={group} onChange={(event) => setGroup(event.target.value)} placeholder="rg-aks" /></Field>
      <Field label={t.createName}><input value={name} onChange={(event) => setName(event.target.value)} aria-invalid={!isGatewayName(name.trim())} /></Field>
      <Field label={t.createLocation}><input value={location} onChange={(event) => setLocation(event.target.value)} /></Field>
      <Field label={t.aksNamespace}><input value={namespace} onChange={(event) => setNamespace(event.target.value)} /></Field>
    </div>
    <ErrorText error={bundle.error} />
    {bundle.data && <AksFiles bundle={bundle.data} />}
    <div className="gateway-form-actions">
      <Button onClick={onCancel}>{t.back}</Button>
      <Button variant={bundle.data ? "outline" : "primary"} disabled={!valid || bundle.isPending} onClick={() => bundle.mutate()}>
        {bundle.isPending ? <LoaderCircle className="spin" size={13} /> : <Download size={13} />}{t.aksGenerate}
      </Button>
      {bundle.data && <Button variant="primary" onClick={() => onConnect(name.trim())}><Link2 size={13} />{t.aksConnect}</Button>}
    </div>
  </section>
}

function AksFiles({ bundle }: { bundle: AksBundle }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(bundle.files[0]?.path ?? "")
  const download = (path: string, content: string) => {
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }))
    const link = document.createElement("a")
    link.href = url
    link.download = path
    link.click()
    URL.revokeObjectURL(url)
  }
  const file = bundle.files.find((item) => item.path === open) ?? bundle.files[0]
  return <div className="aks-files">
    <div className="segment code-tabs" role="tablist">
      {bundle.files.map((item) => <button key={item.path} type="button" role="tab" aria-selected={item.path === file?.path} className={item.path === file?.path ? "active" : ""} onClick={() => setOpen(item.path)}>{item.path}</button>)}
    </div>
    {file && <div className="code-block">
      <pre><code>{file.content}</code></pre>
      <CopyButton text={file.content} label={file.path} />
    </div>}
    <div><Button variant="ghost" onClick={() => bundle.files.forEach((item) => download(item.path, item.content))}><Download size={13} />{t.aksDownload}</Button></div>
  </div>
}

// -- connecting a gateway this app didn't deploy -------------------------------------------

function ConnectGatewayForm({ tenantId, subscriptionId, initialName, onConnected, onCancel }: {
  tenantId: string | null
  subscriptionId: string
  initialName?: string
  onConnected: () => void
  onCancel: () => void
}) {
  const { t } = useI18n()
  const switched = useSwitched()
  const [name, setName] = useState(initialName ?? "")
  const [url, setUrl] = useState("")
  const [key, setKey] = useState("")
  const [principal, setPrincipal] = useState("")
  const [insights, setInsights] = useState("")
  const connect = useMutation({
    mutationFn: () => api.connectGateway({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      name: name.trim(),
      url: url.trim().replace(/\/+$/, ""),
      master_key: key,
      principal_id: principal.trim() || null,
      app_insights_id: insights.trim() || null,
    }),
    onSuccess: (view) => { chooseTarget(targetFromView(view)); onConnected(); return switched() },
  })
  const valid = Boolean(name.trim()) && /^https?:\/\/[^/]+\/?$/.test(url.trim()) && key.length >= 8
    && (!principal.trim() || GUID.test(principal.trim()))
    && (!insights.trim() || APP_INSIGHTS_ID.test(insights.trim()))
  return <form className="gateway-form" onSubmit={(event) => { event.preventDefault(); if (valid) connect.mutate() }}>
    <h3>{t.connectTitle}</h3>
    <p className="muted">{t.connectHint}</p>
    <div className="form-grid">
      <Field label={t.connectName}><input value={name} onChange={(event) => setName(event.target.value)} placeholder="aks-gateway" /></Field>
      <Field label={t.connectUrl} hint={t.connectUrlHint}><input value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://gateway.contoso.com" /></Field>
      <Field label={t.connectKey} hint={t.connectKeyHint}><input type="password" autoComplete="off" value={key} onChange={(event) => setKey(event.target.value)} placeholder="sk-..." /></Field>
      <Field label={t.connectPrincipal} hint={t.connectPrincipalHint}><input value={principal} onChange={(event) => setPrincipal(event.target.value)} aria-invalid={Boolean(principal.trim()) && !GUID.test(principal.trim())} /></Field>
      <div className="span-2"><Field label={t.connectInsights} hint={t.connectInsightsHint}><input value={insights} onChange={(event) => setInsights(event.target.value)} aria-invalid={Boolean(insights.trim()) && !APP_INSIGHTS_ID.test(insights.trim())} /></Field></div>
    </div>
    <ErrorText error={connect.error} />
    <div className="gateway-form-actions">
      <Button onClick={onCancel} disabled={connect.isPending}>{t.back}</Button>
      <Button type="submit" variant="primary" disabled={!valid || connect.isPending}>
        {connect.isPending ? <LoaderCircle className="spin" size={13} /> : <Plug size={13} />}{t.connectSubmit}
      </Button>
    </div>
  </form>
}

// -- deleting ---------------------------------------------------------------------------------

/** Shows what deleting removes and asks for the gateway name before deleting it. */
export function DeleteGatewayDialog({ gateway, tenantId, subscriptionId, onClose, onDeleted }: {
  gateway: Pick<GatewayView, "id" | "name" | "resource_group">
  tenantId: string | null
  subscriptionId: string
  onClose: () => void
  onDeleted?: () => void
}) {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const switched = useSwitched()
  const [typed, setTyped] = useState("")
  const target = { tenant_id: tenantId, subscription_id: subscriptionId, resource_group: gateway.resource_group, gateway_name: gateway.name }
  // A POST that only reads; it needs the CSRF header like every non-GET.
  const check = useQuery({
    // Outside ["target"], so refreshing the lists after a delete doesn't re-check a deleted gateway.
    queryKey: ["delete-check", gateway.id],
    queryFn: () => api.deleteCheck(target),
    retry: false,
    staleTime: 0,
  })
  const remove = useMutation({
    mutationFn: () => api.deleteGateway({ ...target, confirm_name: typed.trim() }),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ["delete-check", gateway.id] })
      onDeleted?.()
      onClose()
      // The current gateway is gone: everything else reloads and setup asks for another one.
      if (check.data?.current) {
        chooseTarget(null)
        await switched()
      }
      else await queryClient.invalidateQueries({ queryKey: ["target"] })
    },
  })
  const confirmed = typed.trim().toLowerCase() === gateway.name.toLowerCase()
  return <Dialog
    open
    onClose={() => { if (!remove.isPending) onClose() }}
    title={fill(t.deleteTitle, { name: gateway.name })}
    footer={<>
      <Button disabled={remove.isPending} onClick={onClose}>{t.cancel}</Button>
      <Button variant="destructive" disabled={!confirmed || !check.data || remove.isPending} onClick={() => remove.mutate()}>
        {remove.isPending ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}{t.deleteConfirm}
      </Button>
    </>}
  >
    <div className="delete-service">
      {check.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.deleteLoading}</div>}
      <ErrorText error={check.error} />
      {check.data && <>
        <div className="callout warning"><CircleAlert size={15} /><div>
          <span>{fill(check.data.deployed ? t.deleteLoss : t.deleteLossFailed, { name: gateway.name, count: check.data.resources.length }, locale)}</span>
          {check.data.current && <b>{t.deleteCurrent}</b>}
        </div></div>
        <ul className="delete-resources">{check.data.resources.map((resource) => <li key={resource}><code>{resource}</code></li>)}</ul>
        <p className="muted">{t.deleteNoRestore}</p>
        {check.data.resources.some((resource) => resource.startsWith("Microsoft.App/managedEnvironments/")) && <p className="muted">{t.deleteBackground}</p>}
        <label className="field">{fill(t.deleteConfirmLabel, { name: gateway.name })}
          <input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} placeholder={gateway.name} />
        </label>
      </>}
      <ErrorText error={remove.error} />
    </div>
  </Dialog>
}
