// Opened from the APIM name in the top bar: the service the app manages, what it costs, and how
// to remove what the app created in it, or (locally, for services the app created) the service.
import { Eraser, ExternalLink, LoaderCircle, RefreshCw, Server, Trash2 } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { COST_NOTE, formatMoney } from "../cost"
import { fill, useI18n } from "../i18n"
import { DeleteServiceDialog } from "./apim-picker"
import { CleanupDialog } from "./removal-dialog"
import { Button, Dialog, ErrorText } from "../components/ui"

/** This month's spend and the tier's list price; read only while the panel is open. */
function ServiceCostSection() {
  const { t, locale } = useI18n("apim")
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
                {price.monthly !== null && <b>{fill(t.costPerMonth, { amount: formatMoney(price.monthly, price.currency, locale, 0) })}</b>}
                <small className="muted">
                  {fill(t.costPlan, { tier: price.tier, units: price.units, region: data.location })}
                  {price.per_10k_calls !== null && ` · ${fill(t.costCalls, { amount: formatMoney(price.per_10k_calls, price.currency, locale, 3) })}`}
                </small>
              </>
              : <span className="muted">{t.costListUnavailable}</span>}
            </dd>
          </div>
        </dl>
        <p className="muted">{t.costSources}</p>
        <a href={data.portal_url} target="_blank" rel="noreferrer"><ExternalLink size={13} />{t.costOpenAnalysis}</a>
        <ErrorText error={refresh.error} />
      </>}
  </section>
}

export function ServicePanel({ onClose }: { onClose: () => void }) {
  const { t } = useI18n("apim")
  const me = useQuery(queries.me())
  const capabilities = useQuery(queries.capabilities())
  const target = useQuery(queries.target())
  const [action, setAction] = useState<"cleanup" | "delete" | null>(null)
  const service = capabilities.data
  if (!service) return null
  const back = () => setAction(null)

  if (action === "cleanup") return <CleanupDialog apimName={service.apim_name} onClose={back} />
  if (action === "delete" && target.data?.subscription_id) {
    return <DeleteServiceDialog
      service={{ id: service.apim_resource_id, name: service.apim_name, resource_group: service.resource_group }}
      tenantId={target.data.tenant_id ?? null}
      subscriptionId={target.data.subscription_id}
      onClose={back}
      // The service is gone, and with it everything this panel describes.
      onDeleted={onClose}
    />
  }
  // Deleting is local-only and refused for a service fixed by the environment or deployment.
  const canDelete = target.data?.can_change === true
  return <Dialog
    open
    onClose={onClose}
    title={<span className="service-title"><Server size={16} />{fill(t.serviceTitle, { name: service.apim_name })}</span>}
    footer={<Button onClick={onClose}>{t.close}</Button>}
  >
    <div className="service-panel">
      <dl>
        <div><dt>{t.createGroup}</dt><dd>{service.resource_group}</dd></div>
        {service.gateway_url && <div><dt>{t.serviceGateway}</dt><dd><code>{service.gateway_url}</code></dd></div>}
      </dl>
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
