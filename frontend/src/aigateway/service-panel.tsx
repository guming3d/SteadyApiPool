// Opened from the gateway name in the top bar: the AI Gateway the app manages, switching to
// another one, what it costs, and how to remove what the app created on it, or (locally, for
// gateways the app created) the gateway itself.
import { ArrowLeftRight, Eraser, ExternalLink, LoaderCircle, RefreshCw, Server, Trash2 } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { COST_NOTE, formatMoney } from "../cost"
import { fill, useI18n } from "../i18n"
import { DeleteGatewayDialog } from "./gateway-picker"
import { CleanupDialog } from "./removal-dialog"
import { Button, Dialog, ErrorText } from "../components/ui"

const PORTAL = "https://portal.azure.com/#resource"

/** This month's spend and the request price; read only while the panel is open. */
function ServiceCostSection() {
  const { t, locale } = useI18n("aigateway")
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
                <b>{price.per_10k_requests > 0
                  ? fill(t.costPer10k, { amount: formatMoney(price.per_10k_requests, price.currency, locale) })
                  : t.costNoCharge}</b>
                <small className="muted">{fill(t.costPlan, { sku: data.sku ?? "—", region: data.location ?? "—" })}</small>
              </>
              : <span className="muted">{t.costListUnavailable}</span>}
            </dd>
          </div>
        </dl>
        <p className="muted">{t.costSources}</p>
        {data.portal_url && <a href={data.portal_url} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.costOpenAnalysis}</a>}
        <ErrorText error={refresh.error} />
      </>}
  </section>
}

export function ServicePanel({ onClose, onSwitch }: { onClose: () => void; onSwitch: () => void }) {
  const { t } = useI18n("aigateway")
  const me = useQuery(queries.me())
  const capabilities = useQuery(queries.capabilities())
  const target = useQuery(queries.target())
  const [action, setAction] = useState<"cleanup" | "delete" | null>(null)
  const service = capabilities.data
  if (!service) return null
  const back = () => setAction(null)

  if (action === "cleanup") return <CleanupDialog gatewayName={service.gateway_name} onClose={back} />
  if (action === "delete" && target.data?.subscription_id) {
    return <DeleteGatewayDialog
      gateway={{ id: service.gateway_resource_id, name: service.gateway_name, resource_group: service.resource_group }}
      tenantId={target.data.tenant_id ?? null}
      subscriptionId={target.data.subscription_id}
      onClose={back}
      // The gateway is gone, and with it everything this panel describes.
      onDeleted={onClose}
    />
  }
  // Deleting is local-only, for a gateway the environment doesn't fix; the backend checks it is
  // one this app created.
  const canDelete = target.data?.can_change === true
  return <Dialog
    open
    onClose={onClose}
    title={<span className="service-title"><Server size={16} />{fill(t.serviceTitle, { name: service.gateway_name })}</span>}
    footer={<Button onClick={onClose}>{t.close}</Button>}
  >
    <div className="service-panel">
      <dl>
        <div><dt>{t.createGroup}</dt><dd>{service.resource_group}</dd></div>
        {service.location && <div><dt>{t.createLocation}</dt><dd>{service.location}{service.sku ? ` · ${service.sku}` : ""}</dd></div>}
        {service.gateway_url && <div><dt>{t.serviceGateway}</dt><dd><code>{service.gateway_url}</code></dd></div>}
        <div><dt>{t.serviceIdentity}</dt><dd>{service.gateway_principal_id ? <code>{service.gateway_principal_id}</code> : <span className="muted">{t.identityMissing}</span>}</dd></div>
        <div><dt>{t.serviceTelemetry}</dt><dd>{service.app_insights_id
          ? <a href={`${PORTAL}${service.app_insights_id}/overview`} target="_blank" rel="noreferrer"><ExternalLink size={13} />{service.app_insights_id.split("/").pop()}</a>
          : <span className="muted">{t.serviceTelemetryNone}</span>}</dd></div>
        <div><dt>{t.servicePortal}</dt><dd>
          <a href="https://ai.gateway.azure.com" target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.servicePortalOpen}</a>
          {" · "}
          <a href={`${PORTAL}${service.gateway_resource_id}/overview`} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.serviceAzurePortal}</a>
        </dd></div>
      </dl>
      {target.data?.source !== "guest" && <section className="service-action">
        <div><b>{t.serviceSwitch}</b><span className="muted">{target.data?.can_change ? t.serviceSwitchHint : t.serviceSwitchFixed}</span></div>
        {target.data?.can_change && <Button onClick={onSwitch}><ArrowLeftRight size={13} />{t.serviceSwitchOpen}</Button>}
      </section>}
      <ServiceCostSection />
      <section className="service-action">
        <div><b>{t.serviceCleanup}</b><span className="muted">{me.data?.can_write ? t.serviceCleanupHint : t.serviceReadOnly}</span></div>
        <Button disabled={!me.data?.can_write} onClick={() => setAction("cleanup")}><Eraser size={13} />{t.serviceCleanupOpen}</Button>
      </section>
      <section className="service-action" data-tone="danger">
        <div><b>{t.serviceRemove}</b><span className="muted">{canDelete ? t.serviceRemoveHint : t.serviceRemoveFixed}</span></div>
        {canDelete && <Button variant="destructive" onClick={() => setAction("delete")}><Trash2 size={13} />{t.targetDelete}</Button>}
      </section>
    </div>
  </Dialog>
}
