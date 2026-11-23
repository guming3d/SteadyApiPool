// Choose the AI Gateway to manage from the user's subscriptions, or create one (local setup only):
// a Standard gateway with a system-assigned identity, tagged as this app's. Deleting is offered
// only for gateways this app created, with the name typed again.
import { CircleAlert, ExternalLink, LoaderCircle, Plus, Sparkles, Trash2 } from "lucide-react"
import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError, gatewaySwitched } from "../api"
import { api, queries } from "./api"
import { fill, useI18n } from "../i18n"
import { targetFor } from "../product"
import { GATEWAY_NAME_PATTERN, RESOURCE_GROUP_PATTERN, suggestGatewayName } from "./pool-ids"
import type { SetupIdentity, TargetView } from "../types"
import type { GatewayView } from "./types"
import { SwitchRefused } from "../components/setup"
import { Button, Dialog, ErrorText } from "../components/ui"

const PREFERRED_LOCATIONS = ["eastus2", "swedencentral", "westus3", "eastus", "westeurope"]

/** Everything shown depends on the gateway: drop it and check the new one from scratch. */
function useSwitched() {
  const queryClient = useQueryClient()
  return () => gatewaySwitched(queryClient)
}

export function GatewayPicker({ target: chosen, user }: { target: TargetView; user: SetupIdentity | null }) {
  const target = targetFor("aigateway", chosen)
  const { t } = useI18n("aigateway")
  const tenants = useQuery(queries.tenants())
  const [tenantId, setTenantId] = useState<string | null>(target.tenant_id ?? user?.tenant_id ?? null)
  const [subscriptionId, setSubscriptionId] = useState(target.subscription_id ?? "")
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<GatewayView | null>(null)
  const subscriptions = useQuery(queries.subscriptions(tenantId))
  const gateways = useQuery(queries.gateways(tenantId, subscriptionId))
  const switched = useSwitched()
  const queryClient = useQueryClient()
  const signIn = useMutation({
    mutationFn: (tenant: string | null) => api.signIn(tenant ?? undefined),
    // Another tenant's subscriptions to list; the gateway stays.
    onSuccess: () => queryClient.invalidateQueries(),
  })
  // The backend refuses a gateway still being created or failed (424); force chooses it anyway,
  // to follow it in setup.
  const select = useMutation({
    mutationFn: ({ gateway, force = false }: { gateway: GatewayView; force?: boolean }) => api.selectTarget({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: gateway.resource_group,
      aigateway_name: gateway.name,
      force,
    }),
    onSuccess: () => switched(),
  })

  useEffect(() => {
    if (!tenantId && tenants.data?.length) setTenantId(tenants.data[0]!.tenant_id)
  }, [tenantId, tenants.data])
  useEffect(() => {
    const enabled = subscriptions.data?.filter((item) => item.state === "Enabled") ?? []
    if (enabled.length && !enabled.some((item) => item.subscription_id === subscriptionId)) {
      setSubscriptionId(enabled[0]!.subscription_id)
    }
  }, [subscriptions.data, subscriptionId])

  const needsSignIn = subscriptions.error instanceof ApiError && subscriptions.error.status === 401
  return <div className="apim-picker">
    <div className="form-grid">
      {(tenants.data?.length ?? 0) > 1 && <label className="field">{t.targetTenant}
        <select value={tenantId ?? ""} onChange={(event) => { setTenantId(event.target.value); setSubscriptionId(""); setCreating(false) }}>
          {tenants.data!.map((tenant) => <option key={tenant.tenant_id} value={tenant.tenant_id}>
            {tenant.display_name ?? tenant.tenant_id}{tenant.default_domain ? ` (${tenant.default_domain})` : ""}
          </option>)}
        </select>
      </label>}
      <label className="field">{t.targetSubscription}
        <select value={subscriptionId} disabled={!subscriptions.data?.length} onChange={(event) => { setSubscriptionId(event.target.value); setCreating(false) }}>
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
          {signIn.isPending ? <LoaderCircle className="spin" size={13} /> : <ExternalLink size={13} />}{t.setupSignInBrowser}
        </Button></div>
      </div></div>
      : <ErrorText error={tenants.error ?? subscriptions.error} />}
    {subscriptionId && !creating && <>
      {gateways.isLoading
        ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
        : <ErrorText error={gateways.error} />}
      {gateways.data && (gateways.data.length
        ? <ul className="apim-services">{gateways.data.map((gateway) => {
          const current = gateway.name.toLowerCase() === target.gateway_name?.toLowerCase()
            && gateway.resource_group.toLowerCase() === target.resource_group?.toLowerCase()
          return <li key={gateway.id}>
            <Sparkles size={15} />
            <div>
              <b>{gateway.name}</b>
              <span className="muted">{gateway.resource_group} · {gateway.location} · {gateway.sku}
                {gateway.provisioning_state && gateway.provisioning_state !== "Succeeded" && ` · ${gateway.provisioning_state}`}</span>
              <span>
                {gateway.managed && <span className="tag">{t.targetManaged}</span>}
                {gateway.can_manage === true && <span className="tag" data-tone="ok">{t.targetCanManage}</span>}
                {gateway.can_manage === false && <span className="tag" data-tone="warn" title={gateway.missing.join("\n")}>{t.targetCannotManage}</span>}
              </span>
            </div>
            <div className="apim-service-actions">
              {/* Only gateways this app created can be deleted here; the backend checks too. */}
              {gateway.managed && <Button variant="ghost" className="danger" onClick={() => setDeleting(gateway)}>
                <Trash2 size={13} />{t.targetDelete}
              </Button>}
              {current
                ? <span className="tag" data-tone="ok">{t.targetInUse}</span>
                : <Button disabled={select.isPending} onClick={() => select.mutate({ gateway })}>
                  {select.isPending && select.variables?.gateway.id === gateway.id && <LoaderCircle className="spin" size={13} />}{t.targetUse}
                </Button>}
            </div>
          </li>
        })}</ul>
        : <div className="state">{t.targetNoServices}</div>)}
      {select.error
        ? <SwitchRefused error={select.error} name={select.variables?.gateway.name} busy={select.isPending}
          onForce={() => select.variables && select.mutate({ gateway: select.variables.gateway, force: true })} />
        : <ErrorText error={signIn.error} />}
      {gateways.data && <div><Button onClick={() => setCreating(true)}><Plus size={13} />{t.targetCreate}</Button></div>}
    </>}
    {deleting && <DeleteGatewayDialog gateway={deleting} tenantId={tenantId} subscriptionId={subscriptionId} onClose={() => setDeleting(null)} />}
    {subscriptionId && creating && <CreateGatewayForm tenantId={tenantId} subscriptionId={subscriptionId} onCancel={() => setCreating(false)} />}
  </div>
}

/** Shows what deleting removes and asks for the gateway name before deleting it. */
export function DeleteGatewayDialog({ gateway, tenantId, subscriptionId, onClose, onDeleted }: {
  gateway: Pick<GatewayView, "id" | "name" | "resource_group">
  tenantId: string | null
  subscriptionId: string
  onClose: () => void
  onDeleted?: () => void
}) {
  const { t, locale } = useI18n("aigateway")
  const queryClient = useQueryClient()
  const switched = useSwitched()
  const [typed, setTyped] = useState("")
  const target = { tenant_id: tenantId, subscription_id: subscriptionId, resource_group: gateway.resource_group, aigateway_name: gateway.name }
  // A POST that only reads; it needs the CSRF header like every non-GET.
  const check = useQuery({
    // Outside ["aigateway", "target"], so refreshing the lists doesn't re-check a deleted gateway.
    queryKey: ["aigateway-delete-check", gateway.id],
    queryFn: () => api.deleteCheck(target),
    retry: false,
    staleTime: 0,
  })
  const remove = useMutation({
    mutationFn: () => api.deleteGateway({ ...target, confirm_name: typed.trim() }),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ["aigateway-delete-check", gateway.id] })
      onDeleted?.()
      onClose()
      // The current gateway is gone: everything else reloads and setup asks for another one.
      if (check.data?.current) await switched()
      else await queryClient.invalidateQueries({ queryKey: ["aigateway", "target"] })
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
          <span>{fill(t.deleteLoss, { name: gateway.name, pools: check.data.pools, members: check.data.members, keys: check.data.keys }, locale)}</span>
          {check.data.current && <b>{t.deleteCurrent}</b>}
        </div></div>
        <p className="muted">{t.deleteNameKept}</p>
        <label className="field">{fill(t.deleteConfirmLabel, { name: gateway.name })}
          <input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} placeholder={gateway.name} />
        </label>
      </>}
      <ErrorText error={remove.error} />
    </div>
  </Dialog>
}

function CreateGatewayForm({ tenantId, subscriptionId, onCancel }: {
  tenantId: string | null
  subscriptionId: string
  onCancel: () => void
}) {
  const { t } = useI18n("aigateway")
  const options = useQuery(queries.createOptions(tenantId, subscriptionId))
  const switched = useSwitched()
  const [name, setName] = useState(() => suggestGatewayName())
  const [group, setGroup] = useState("rg-ai-gateway")
  const [location, setLocation] = useState("")
  const create = useMutation({
    mutationFn: () => api.createGateway({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: group.trim(),
      location,
      name: name.trim(),
    }),
    onSuccess: () => switched(),
  })

  const existingGroup = options.data?.resource_groups.find((item) => item.name.toLowerCase() === group.trim().toLowerCase())
  const locations = options.data?.locations ?? []
  useEffect(() => {
    // Keep a valid region: the existing group's, else a common one, else the first offered.
    if (locations.includes(location)) return
    const preferred = [existingGroup?.location, ...PREFERRED_LOCATIONS].find((item) => item && locations.includes(item))
    setLocation(preferred ?? locations[0] ?? "")
  }, [locations, location, existingGroup])

  if (options.isLoading) return <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
  if (!options.data) return <ErrorText error={options.error} />
  const nameValid = GATEWAY_NAME_PATTERN.test(name.trim())
  const valid = nameValid && RESOURCE_GROUP_PATTERN.test(group.trim()) && Boolean(location)
  return <form className="apim-create" onSubmit={(event) => { event.preventDefault(); if (valid) create.mutate() }}>
    <h3>{t.targetCreate}</h3>
    <div className="form-grid">
      <label className="field">{t.createName}
        <input value={name} onChange={(event) => setName(event.target.value)} aria-invalid={!nameValid} />
        <small>{nameValid ? fill(t.createNameHint, { name: name.trim() }) : t.createNameInvalid}</small>
      </label>
      <label className="field">{t.createGroup}
        <input value={group} list="aigw-create-groups" onChange={(event) => setGroup(event.target.value)} aria-invalid={!RESOURCE_GROUP_PATTERN.test(group.trim())} />
        <datalist id="aigw-create-groups">{options.data.resource_groups.map((item) => <option key={item.name} value={item.name} />)}</datalist>
        <small>{existingGroup ? fill(t.createGroupExisting, { location: existingGroup.location }) : t.createGroupNew}</small>
      </label>
      <label className="field">{t.createLocation}
        <select value={location} disabled={!locations.length} onChange={(event) => setLocation(event.target.value)}>
          {!locations.length && <option value="">{t.createNoLocations}</option>}
          {locations.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
        <small>{t.createSkuHint}</small>
      </label>
    </div>
    {!options.data.provider_registered && <div className="state">{t.createRegisterProvider}</div>}
    <div className="callout warning"><CircleAlert size={15} /><div><span>{t.createBilling}</span></div></div>
    <ErrorText error={create.error} />
    <div className="apim-create-actions">
      <Button onClick={onCancel} disabled={create.isPending}>{t.cancel}</Button>
      <Button type="submit" variant="primary" disabled={!valid || create.isPending}>
        {create.isPending ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{t.createSubmit}
      </Button>
    </div>
  </form>
}
