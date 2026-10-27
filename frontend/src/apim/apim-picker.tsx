// Choose the APIM service to manage from the user's subscriptions, or create one: locally, and in
// the public app, where the browser remembers the choice (src/session.ts).
import { CircleAlert, ExternalLink, LoaderCircle, Plus, RotateCcw, Server, Trash2 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { ApiError } from "../api"
import { api, queries } from "./api"
import { fill, useI18n } from "../i18n"
import { targetFor } from "../product"
import { targetFromServiceId, targetFromView } from "../public-target"
import { chooseTarget, isPublic, signIn as publicSignIn } from "../session"
import type { SetupIdentity, TargetView } from "../types"
import type { ApimServiceView, ApimSku, DeletedServiceView } from "./types"
import { Button, Dialog, ErrorText } from "../components/ui"

const SKUS: ApimSku[] = ["BasicV2", "StandardV2", "Developer"]
// Mirrors APIM_NAME_PATTERN in pool_manager/models.py; the name becomes <name>.azure-api.net.
const APIM_NAME = /^[A-Za-z](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$/
const RESOURCE_GROUP = /^[-\w.()]{0,89}[-\w()]$/
const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/
const PREFERRED_LOCATIONS = ["eastus2", "swedencentral", "eastus", "westeurope"]

/** Everything shown depends on the APIM service, so reload it all after a switch. */
function useSwitched() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries()
}

export function ApimPicker({ target: chosen, user }: { target: TargetView; user: SetupIdentity | null }) {
  const target = targetFor("apim", chosen)
  const { t } = useI18n("apim")
  const tenants = useQuery(queries.tenants())
  const [tenantId, setTenantId] = useState<string | null>(target.tenant_id ?? user?.tenant_id ?? null)
  const [subscriptionId, setSubscriptionId] = useState(target.subscription_id ?? "")
  const [creating, setCreating] = useState(false)
  const [deleting, setDeleting] = useState<ApimServiceView | null>(null)
  const subscriptions = useQuery(queries.subscriptions(tenantId))
  const services = useQuery(queries.services(tenantId, subscriptionId))
  const switched = useSwitched()
  const signIn = useMutation({
    // The public app signs in to that tenant with a full-page redirect.
    mutationFn: (tenant: string | null) => isPublic() ? publicSignIn({ tenantId: tenant }) : api.signIn(tenant ?? undefined),
    onSuccess: switched,
  })
  const select = useMutation({
    mutationFn: (service: ApimServiceView) => api.selectTarget({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: service.resource_group,
      apim_name: service.name,
    }),
    onSuccess: (view) => { chooseTarget(targetFromView(view)); return switched() },
  })

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
          {signIn.isPending ? <LoaderCircle className="spin" size={13} /> : <ExternalLink size={13} />}{isPublic() ? t.setupSignInTenant : t.setupSignInBrowser}
        </Button></div>
      </div></div>
      : <ErrorText error={tenants.error ?? subscriptions.error} />}
    {subscriptionId && !creating && <>
      {services.isLoading
        ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
        : <ErrorText error={services.error} />}
      {services.data && (services.data.length
        ? <ul className="apim-services">{services.data.map((service) => {
          const current = service.name.toLowerCase() === target.gateway_name?.toLowerCase()
            && service.resource_group.toLowerCase() === target.resource_group?.toLowerCase()
          return <li key={service.id}>
            <Server size={15} />
            <div>
              <b>{service.name}</b>
              <span className="muted">{service.resource_group} · {service.location} · {service.sku}
                {service.provisioning_state && service.provisioning_state !== "Succeeded" && ` · ${service.provisioning_state}`}</span>
              <span>
                {service.managed && <span className="tag">{t.targetManaged}</span>}
                {service.can_manage === true && <span className="tag" data-tone="ok">{t.targetCanManage}</span>}
                {service.can_manage === false && <span className="tag" data-tone="warn" title={service.missing.join("\n")}>{t.targetCannotManage}</span>}
              </span>
            </div>
            <div className="apim-service-actions">
              {/* Only services this app created can be deleted here; the backend checks too. */}
              {service.managed && <Button variant="ghost" className="danger" onClick={() => setDeleting(service)}>
                <Trash2 size={13} />{t.targetDelete}
              </Button>}
              {current
                ? <span className="tag" data-tone="ok">{t.targetInUse}</span>
                : <Button disabled={select.isPending} onClick={() => select.mutate(service)}>
                  {select.isPending && select.variables?.id === service.id && <LoaderCircle className="spin" size={13} />}{t.targetUse}
                </Button>}
            </div>
          </li>
        })}</ul>
        : <div className="state">{t.targetNoServices}</div>)}
      <ErrorText error={select.error ?? signIn.error} />
      {services.data && <div><Button onClick={() => setCreating(true)}><Plus size={13} />{t.targetCreate}</Button></div>}
      <DeletedServices tenantId={tenantId} subscriptionId={subscriptionId} />
    </>}
    {deleting && <DeleteServiceDialog service={deleting} tenantId={tenantId} subscriptionId={subscriptionId} onClose={() => setDeleting(null)} />}
    {subscriptionId && creating && <CreateApimForm tenantId={tenantId} subscriptionId={subscriptionId} user={user}
      publisher={tenants.data?.find((tenant) => tenant.tenant_id === tenantId)?.display_name ?? null}
      onCancel={() => setCreating(false)} />}
  </div>
}

const when = (iso: string | null) => iso ? new Date(iso).toLocaleString() : "—"

/** Shows what deleting removes and asks for the service name before soft-deleting it. */
export function DeleteServiceDialog({ service, tenantId, subscriptionId, onClose, onDeleted }: {
  service: Pick<ApimServiceView, "id" | "name" | "resource_group">
  tenantId: string | null
  subscriptionId: string
  onClose: () => void
  onDeleted?: () => void
}) {
  const { t, locale } = useI18n("apim")
  const queryClient = useQueryClient()
  const switched = useSwitched()
  const [typed, setTyped] = useState("")
  const target = { tenant_id: tenantId, subscription_id: subscriptionId, resource_group: service.resource_group, apim_name: service.name }
  // A POST that only reads; it needs the CSRF header like every non-GET.
  const check = useQuery({
    // Outside ["target"], so refreshing the lists after a delete doesn't re-check a deleted service.
    queryKey: ["delete-check", service.id],
    queryFn: () => api.deleteCheck(target),
    retry: false,
    staleTime: 0,
  })
  const remove = useMutation({
    mutationFn: () => api.deleteService({ ...target, confirm_name: typed.trim() }),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ["delete-check", service.id] })
      onDeleted?.()
      onClose()
      // The current service is gone: everything else reloads and setup asks for another one.
      if (check.data?.current) {
        chooseTarget(null)
        await switched()
      }
      else await queryClient.invalidateQueries({ queryKey: ["target"] })
    },
  })
  const confirmed = typed.trim().toLowerCase() === service.name.toLowerCase()
  return <Dialog
    open
    onClose={() => { if (!remove.isPending) onClose() }}
    title={fill(t.deleteTitle, { name: service.name })}
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
          <span>{fill(t.deleteLoss, { name: service.name, pools: check.data.pools, members: check.data.members, apis: check.data.apis }, locale)}</span>
          {check.data.current && <b>{t.deleteCurrent}</b>}
        </div></div>
        <p className="muted">{t.deleteSoft}</p>
        <label className="field">{fill(t.deleteConfirmLabel, { name: service.name })}
          <input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} placeholder={service.name} />
        </label>
      </>}
      <ErrorText error={remove.error} />
    </div>
  </Dialog>
}

/** Services Azure still keeps after a delete (48 hours), with a way back. */
function DeletedServices({ tenantId, subscriptionId }: { tenantId: string | null; subscriptionId: string }) {
  const { t } = useI18n("apim")
  const queryClient = useQueryClient()
  const deleted = useQuery(queries.deleted(tenantId, subscriptionId))
  const restore = useMutation({
    mutationFn: (item: DeletedServiceView) => api.restoreService({
      tenant_id: tenantId, subscription_id: subscriptionId, location: item.location, name: item.name,
    }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["target"] }),
  })
  if (!deleted.data?.length) return <ErrorText error={deleted.error} />
  return <section className="apim-deleted">
    <header><b>{t.deletedTitle}</b><span className="muted">{t.deletedHint}</span></header>
    <ul className="apim-services">{deleted.data.map((item) => <li key={`${item.location}/${item.name}`}>
      <Server size={15} />
      <div>
        <b>{item.name}</b>
        <span className="muted">{item.resource_group} · {item.display_location} · {fill(t.deletedAt, { date: when(item.deletion_date) })}</span>
        <span className="muted">{fill(t.deletedPurge, { date: when(item.scheduled_purge_date) })}</span>
      </div>
      <div className="apim-service-actions">
        <Button disabled={restore.isPending} onClick={() => restore.mutate(item)}>
          {restore.isPending && restore.variables?.name === item.name ? <LoaderCircle className="spin" size={13} /> : <RotateCcw size={13} />}{t.deletedRestore}
        </Button>
      </div>
    </li>)}</ul>
    {restore.isSuccess && <div className="state">{fill(t.deletedRestoring, { name: restore.data.name })}</div>}
    <ErrorText error={restore.error} />
  </section>
}

function suggestedName() {
  return `apim-pool-${Math.random().toString(36).slice(2, 7)}`
}

function CreateApimForm({ tenantId, subscriptionId, user, publisher, onCancel }: {
  tenantId: string | null
  subscriptionId: string
  user: SetupIdentity | null
  publisher: string | null
  onCancel: () => void
}) {
  const { t } = useI18n("apim")
  const options = useQuery(queries.createOptions(tenantId, subscriptionId))
  const switched = useSwitched()
  const [name, setName] = useState(suggestedName)
  const [group, setGroup] = useState("rg-apim-pool")
  const [sku, setSku] = useState<ApimSku>("BasicV2")
  const [location, setLocation] = useState("")
  const [email, setEmail] = useState(user?.name && EMAIL.test(user.name) ? user.name : "")
  const [publisherName, setPublisherName] = useState(publisher ?? "")
  const create = useMutation({
    mutationFn: () => api.createService({
      tenant_id: tenantId,
      subscription_id: subscriptionId,
      resource_group: group.trim(),
      location,
      name: name.trim(),
      sku,
      publisher_email: email.trim(),
      publisher_name: publisherName.trim(),
    }),
    onSuccess: (service) => { chooseTarget(targetFromServiceId(service.id, tenantId)); return switched() },
  })

  const existingGroup = options.data?.resource_groups.find((item) => item.name.toLowerCase() === group.trim().toLowerCase())
  const locations = useMemo(() => options.data?.locations[sku] ?? [], [options.data, sku])
  useEffect(() => {
    // Keep a valid region: the existing group's, else a common one, else the first offered.
    if (locations.includes(location)) return
    const preferred = [existingGroup?.location, ...PREFERRED_LOCATIONS].find((item) => item && locations.includes(item))
    setLocation(preferred ?? locations[0] ?? "")
  }, [locations, location, existingGroup])

  if (options.isLoading) return <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
  if (!options.data) return <ErrorText error={options.error} />
  const valid = APIM_NAME.test(name.trim()) && RESOURCE_GROUP.test(group.trim()) && Boolean(location)
    && EMAIL.test(email.trim()) && Boolean(publisherName.trim())
  return <form className="apim-create" onSubmit={(event) => { event.preventDefault(); if (valid) create.mutate() }}>
    <h3>{t.targetCreate}</h3>
    <div className="form-grid">
      <label className="field">{t.createName}
        <input value={name} onChange={(event) => setName(event.target.value)} aria-invalid={!APIM_NAME.test(name.trim())} />
        <small>{APIM_NAME.test(name.trim()) ? fill(t.createNameHint, { name: name.trim() }) : t.createNameInvalid}</small>
      </label>
      <label className="field">{t.createGroup}
        <input value={group} list="apim-create-groups" onChange={(event) => setGroup(event.target.value)} aria-invalid={!RESOURCE_GROUP.test(group.trim())} />
        <datalist id="apim-create-groups">{options.data.resource_groups.map((item) => <option key={item.name} value={item.name} />)}</datalist>
        <small>{existingGroup ? fill(t.createGroupExisting, { location: existingGroup.location }) : t.createGroupNew}</small>
      </label>
      <label className="field">{t.createSku}
        <select value={sku} onChange={(event) => setSku(event.target.value as ApimSku)}>
          {SKUS.map((item) => <option key={item} value={item}>{t[`sku${item}`]}</option>)}
        </select>
        <small>{t[`sku${sku}Hint`]}</small>
      </label>
      <label className="field">{t.createLocation}
        <select value={location} disabled={!locations.length} onChange={(event) => setLocation(event.target.value)}>
          {!locations.length && <option value="">{t.createNoLocations}</option>}
          {locations.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <label className="field">{t.createPublisherEmail}
        <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} aria-invalid={!EMAIL.test(email.trim())} />
        <small>{t.createPublisherEmailHint}</small>
      </label>
      <label className="field">{t.createPublisherName}
        <input value={publisherName} onChange={(event) => setPublisherName(event.target.value)} />
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
