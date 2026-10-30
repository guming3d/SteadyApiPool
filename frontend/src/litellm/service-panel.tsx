// Opened from the gateway name in the top bar: the gateway the app manages, switching to another
// one, what it costs, and how to remove what the app created on it, or (locally, for gateways the
// app deployed) the gateway.
import { ArrowLeftRight, Eraser, ExternalLink, Gauge, LoaderCircle, RefreshCw, Server, Trash2 } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { containerAppPortalUrl, masterKeyCommand } from "./aca-maintenance"
import { clusterPortalUrl, maintenanceCommands } from "./aks-maintenance"
import { api, keys, queries } from "./api"
import { COST_NOTE, formatMoney } from "../cost"
import { fill, useI18n } from "../i18n"
import { CopyButton } from "./call-guide"
import { DeleteGatewayDialog } from "./gateway-picker"
import { CleanupDialog } from "./removal-dialog"
import { ResizeDialog } from "./resize-dialog"
import { Button, Dialog, ErrorText } from "../components/ui"

/** This month's spend and a list price estimate; read only while the panel is open. */
function ServiceCostSection() {
  const { t, locale } = useI18n("litellm")
  const queryClient = useQueryClient()
  const cost = useQuery(queries.cost())
  const refresh = useMutation({
    mutationFn: () => api.serviceCost(true),
    onSuccess: (fresh) => queryClient.setQueryData(keys.cost, fresh),
  })
  const busy = cost.isFetching || refresh.isPending
  const data = cost.data
  const spend = data?.month_to_date
  const price = data?.list_price
  return <section className="service-cost" aria-label={t.costTitle} aria-busy={busy}>
    <header>
      <b>{t.costTitle}</b>
      <button type="button" className="icon-button" title={t.refresh} aria-label={t.refresh} disabled={busy} onClick={() => refresh.mutate()}>
        <RefreshCw size={13} className={busy ? "spin" : ""} />
      </button>
    </header>
    {cost.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.costLoading}</div>
      : !data ? <ErrorText error={cost.error ?? refresh.error} />
      : <>
        <dl>
          <div>
            <dt>{t.costMonthToDate}</dt>
            <dd>{spend
              ? <b>{spend.currency ? formatMoney(spend.amount, spend.currency, locale) : spend.amount.toLocaleString()}</b>
              : <span className="muted" title={data.detail ?? undefined}>{t[COST_NOTE[data.month_to_date_note ?? "unavailable"]]}</span>}
            </dd>
          </div>
          <div>
            <dt>{t.costListPrice}</dt>
            <dd>{price
              ? <>
                <b>{Math.round(price.monthly_low) === Math.round(price.monthly_high)
                  ? fill(t.costPerMonthOne, { amount: formatMoney(price.monthly_low, price.currency, locale, 0) })
                  : fill(t.costPerMonth, {
                    low: formatMoney(price.monthly_low, price.currency, locale, 0),
                    high: formatMoney(price.monthly_high, price.currency, locale, 0),
                  })}</b>
                <ul className="cost-items">{price.items.map((item) => <li key={item.kind}>
                  <span>{t[`costItem_${item.kind}`]}</span>
                  <small className="muted">{item.detail}</small>
                  <span>{item.monthly_low === item.monthly_high
                    ? formatMoney(item.monthly_low, price.currency, locale, 0)
                    : `${formatMoney(item.monthly_low, price.currency, locale, 0)}–${formatMoney(item.monthly_high, price.currency, locale, 0)}`}</span>
                </li>)}</ul>
                <small className="muted">{fill(t.costPlan, {
                  profile: data.profile ? t[data.profile === "dev" ? "profileDev" : "profileProduction"] : "—",
                  region: data.location ?? "—",
                })}</small>
              </>
              : <span className="muted">{data.month_to_date_note === "external" ? t.costExternal : t.costListUnavailable}</span>}
            </dd>
          </div>
        </dl>
        <p className="muted">{price?.items.some((item) => item.kind === "aks_nodes") ? t.costSourcesAks : t.costSources}</p>
        {data.portal_url && <a href={data.portal_url} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.costOpenAnalysis}</a>}
        <ErrorText error={refresh.error} />
      </>}
  </section>
}

/** An AKS gateway the app deployed: the user maintains its cluster with these. */
function MaintainCluster({ clusterId }: { clusterId: string }) {
  const { t } = useI18n("litellm")
  // The profile the gateway was deployed with (its tag), read with the cost section's query.
  const cost = useQuery(queries.cost())
  return <section className="service-maintain">
    <div>
      <b>{t.serviceMaintainTitle}</b>
      <span className="muted">{t.serviceMaintainHint}</span>
      <a href={clusterPortalUrl(clusterId)} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.serviceMaintainOpen}</a>
    </div>
    <ul className="maintain-commands">
      {maintenanceCommands(clusterId, cost.data?.profile ?? null).map((item) => <li key={item.key}>
        <span>{t[item.key]}</span>
        <div className="code-block"><pre><code>{item.command}</code></pre><CopyButton text={item.command} label={t[item.key]} /></div>
      </li>)}
    </ul>
  </section>
}

/** A Container Apps gateway's master key: the app never shows it, so the user reads it with their
 * own Azure rights, for LiteLLM's admin UI. (On AKS, MaintainCluster has the kubectl command.) */
function ContainerAppMasterKey({ appId }: { appId: string }) {
  const { t } = useI18n("litellm")
  const command = masterKeyCommand(appId)
  if (!command) return null
  return <section className="service-maintain">
    <div>
      <b>{t.serviceMasterKeyTitle}</b>
      <span className="muted">{t.serviceMasterKeyHint}</span>
      <a href={containerAppPortalUrl(appId)} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.serviceMasterKeyPortal}</a>
    </div>
    <ul className="maintain-commands">
      <li>
        <span>{t.maintainMasterKey}</span>
        <div className="code-block"><pre><code>{command}</code></pre><CopyButton text={command} label={t.maintainMasterKey} /></div>
      </li>
    </ul>
  </section>
}

export function ServicePanel({ onClose, onSwitch }: { onClose: () => void; onSwitch: () => void }) {
  const { t } = useI18n("litellm")
  const me = useQuery(queries.me())
  const capabilities = useQuery(queries.capabilities())
  const target = useQuery(queries.target())
  const [action, setAction] = useState<"cleanup" | "delete" | "resize" | null>(null)
  const service = capabilities.data
  if (!service) return null
  const back = () => setAction(null)

  const clusterId = target.data?.cluster_id ?? null
  // A Container App gateway is its app; an AKS gateway the app deployed is its cluster.
  const anchor = clusterId ?? service.gateway_resource_id
  const group = target.data?.resource_group ?? service.resource_group
  if (action === "cleanup") return <CleanupDialog gatewayName={service.gateway_name} onClose={back} />
  if (action === "resize") return <ResizeDialog initial={service.sized_for} onClose={back} />
  if (action === "delete" && target.data?.subscription_id && anchor && group) {
    return <DeleteGatewayDialog
      gateway={{ id: anchor, name: service.gateway_name, resource_group: group }}
      tenantId={target.data.tenant_id ?? null}
      subscriptionId={target.data.subscription_id}
      onClose={back}
      // The gateway is gone, and with it everything this panel describes.
      onDeleted={onClose}
    />
  }
  // Deleting is local-only, for a gateway this app deployed (on Container Apps or AKS) that
  // the environment or deployment doesn't fix; a connected one is only disconnected (in setup).
  const canDelete = target.data?.can_change === true && (service.gateway_kind === "aca" || Boolean(clusterId))
  // A gateway this app deployed, on Container Apps or AKS; the backend checks it is this app's.
  const canResize = service.gateway_kind === "aca" || Boolean(clusterId)
  const sized = service.sized_for
  return <Dialog
    open
    onClose={onClose}
    title={<span className="service-title"><Server size={16} />{fill(t.serviceTitle, { name: service.gateway_name })}</span>}
    footer={<Button onClick={onClose}>{t.close}</Button>}
  >
    <div className="service-panel">
      <dl>
        {service.resource_group && <div><dt>{t.createGroup}</dt><dd>{service.resource_group}</dd></div>}
        {service.gateway_url && <div><dt>{t.serviceGateway}</dt><dd><code>{service.gateway_url}</code></dd></div>}
        {service.litellm_version && <div><dt>{t.serviceVersion}</dt><dd>LiteLLM {service.litellm_version}</dd></div>}
        {service.gateway_url && <div><dt>{t.serviceAdminUi}</dt><dd><a href={`${service.gateway_url}/ui`} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.serviceAdminUiOpen}</a></dd></div>}
      </dl>
      {target.data?.source !== "guest" && <section className="service-action">
        <div><b>{t.serviceSwitch}</b><span className="muted">{target.data?.can_change ? t.serviceSwitchHint : t.serviceSwitchFixed}</span></div>
        {target.data?.can_change && <Button onClick={onSwitch}><ArrowLeftRight size={13} />{t.serviceSwitchOpen}</Button>}
      </section>}
      {service.gateway_kind === "aca" && service.gateway_resource_id && <ContainerAppMasterKey appId={service.gateway_resource_id} />}
      <ServiceCostSection />
      {clusterId && <MaintainCluster clusterId={clusterId} />}
      {canResize && <section className="service-action">
        <div>
          <b>{t.resizeSection}</b>
          <span className="muted">{sized
            ? fill(t.resizeSizedFor, {
              rpm: sized.peak_rpm.toLocaleString(),
              input: sized.input_tokens.toLocaleString(),
              output: sized.output_tokens.toLocaleString(),
              timeout: sized.timeout_seconds,
            })
            : t.resizeNotSized}</span>
        </div>
        <Button disabled={!me.data?.can_write} onClick={() => setAction("resize")}><Gauge size={13} />{t.resizeOpen}</Button>
      </section>}
      <section className="service-action">
        <div><b>{t.serviceCleanup}</b><span className="muted">{me.data?.can_write ? t.serviceCleanupHint : t.serviceReadOnly}</span></div>
        <Button disabled={!me.data?.can_write} onClick={() => setAction("cleanup")}><Eraser size={13} />{t.serviceCleanupOpen}</Button>
      </section>
      <section className="service-action" data-tone="danger">
        <div><b>{t.serviceRemove}</b><span className="muted">{canDelete ? clusterId ? t.serviceRemoveHintAks : t.serviceRemoveHint : service.gateway_kind === "external" ? t.serviceRemoveExternal : t.serviceRemoveFixed}</span></div>
        {canDelete && <Button variant="destructive" onClick={() => setAction("delete")}><Trash2 size={13} />{t.targetDelete}</Button>}
      </section>
    </div>
  </Dialog>
}
