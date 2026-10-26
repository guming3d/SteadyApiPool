// Deploys an OpenAI model into an existing Foundry / Azure OpenAI resource, so a pool can use it
// without a trip to the Foundry portal. Runs with the user's own Azure rights; the server checks
// the account, model, deployment type and capacity again before writing (model_deploy.py).
import { CheckCircle2, CircleAlert, LoaderCircle, Rocket } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"

import { api, queries } from "../api"
import { capacityLimit, clampCapacity, defaultVersion, DEPLOYMENT_NAME_PATTERN, finishedState, modelNames, suggestDeploymentName, versionsOf } from "../deploy-model"
import { fill, useI18n, type MessageKey } from "../i18n"
import type { DeploymentSku, FoundryDeploymentStatus, VersionUpgrade } from "../types"
import { Button, Dialog, ErrorText, Field } from "./ui"

const SKU_LABEL: Record<DeploymentSku, { label: MessageKey; hint: MessageKey }> = {
  GlobalStandard: { label: "skuGlobalStandard", hint: "skuGlobalStandardHint" },
  DataZoneStandard: { label: "skuDataZoneStandard", hint: "skuDataZoneStandardHint" },
  Standard: { label: "skuStandard", hint: "skuStandardHint" },
}
const LIFECYCLE_LABEL: Record<string, MessageKey> = {
  preview: "lifecyclePreview",
  legacy: "lifecycleLegacy",
  deprecating: "lifecycleDeprecating",
}
const UPGRADES: Array<{ value: VersionUpgrade; label: MessageKey }> = [
  { value: "OnceNewDefaultVersionAvailable", label: "upgradeOnNewDefault" },
  { value: "OnceCurrentVersionExpired", label: "upgradeOnExpired" },
  { value: "NoAutoUpgrade", label: "upgradeNever" },
]

export function DeployModelDialog({
  existing,
  onDeployed,
  onClose,
}: {
  /** Deployment names already in each account (lower-case account ID), from the scan. */
  existing: Map<string, string[]>
  onDeployed: (status: FoundryDeploymentStatus) => void
  onClose: () => void
}) {
  const { t, locale } = useI18n()
  const accounts = useQuery(queries.foundryAccounts())
  const [accountId, setAccountId] = useState("")
  const models = useQuery(queries.foundryModels(accountId))
  const [filter, setFilter] = useState("")
  const [modelName, setModelName] = useState("")
  const [version, setVersion] = useState("")
  const [sku, setSku] = useState<DeploymentSku | null>(null)
  const [capacity, setCapacity] = useState(0)
  const [name, setName] = useState("")
  const [nameEdited, setNameEdited] = useState(false)
  const [upgrade, setUpgrade] = useState<VersionUpgrade>("OnceNewDefaultVersionAvailable")
  const [started, setStarted] = useState<FoundryDeploymentStatus | null>(null)

  // The first account until the user picks one.
  useEffect(() => {
    if (!accountId && accounts.data?.length) setAccountId(accounts.data[0]!.id)
  }, [accountId, accounts.data])

  const all = models.data ?? []
  const names = modelNames(all).filter((candidate) => candidate.toLowerCase().includes(filter.trim().toLowerCase()))
  const chosen = all.find((item) => item.name === modelName && item.version === version)
  const option = chosen?.skus.find((item) => item.name === sku)
  const capacityInfo = useQuery(queries.foundryCapacity(accountId, chosen?.name ?? "", chosen?.version ?? ""))
  const available = sku ? capacityInfo.data?.available[sku] : undefined
  const taken = useMemo(() => existing.get(accountId.toLowerCase()) ?? [], [existing, accountId])

  const chooseModel = (next: string) => {
    setModelName(next)
    const preferred = defaultVersion(all, next)
    setVersion(preferred?.version ?? "")
    const first = preferred?.skus[0]
    setSku(first?.name ?? null)
    setCapacity(first?.default_capacity ?? 0)
    if (!nameEdited) setName(next ? suggestDeploymentName(next, taken) : "")
  }
  // A new account resets the model: its list may differ.
  useEffect(() => { setModelName(""); setVersion(""); setSku(null) }, [accountId])
  const chooseVersion = (next: string) => {
    setVersion(next)
    const item = all.find((candidate) => candidate.name === modelName && candidate.version === next)
    const keep = item?.skus.find((candidate) => candidate.name === sku) ?? item?.skus[0]
    setSku(keep?.name ?? null)
    if (keep) setCapacity(clampCapacity(capacity || keep.default_capacity, keep))
  }
  const chooseSku = (next: DeploymentSku) => {
    setSku(next)
    const item = chosen?.skus.find((candidate) => candidate.name === next)
    if (item) setCapacity(clampCapacity(item.default_capacity, item, capacityInfo.data?.available[next]))
  }

  const limit = option ? capacityLimit(option, available) : 0
  const noQuota = available !== undefined && option !== undefined && available < option.min_capacity
  const nameTaken = taken.some((item) => item.toLowerCase() === name.trim().toLowerCase())
  const nameValid = DEPLOYMENT_NAME_PATTERN.test(name.trim())
  const valid = Boolean(chosen && option && nameValid && !nameTaken && !noQuota
    && capacity >= option.min_capacity && capacity <= limit)

  const create = useMutation({
    mutationFn: () => api.createFoundryDeployment({
      account_id: accountId,
      deployment: name.trim(),
      model_name: chosen!.name,
      model_version: chosen!.version,
      sku: sku!,
      capacity,
      version_upgrade: upgrade,
    }),
    onSuccess: setStarted,
  })
  // Azure takes a minute or so; follow it until it settles.
  const status = useQuery({
    queryKey: ["foundry", "deployment", started?.account_id, started?.deployment],
    queryFn: () => api.foundryDeploymentStatus(started!.account_id, started!.deployment),
    enabled: started !== null,
    refetchInterval: (query) => query.state.data && finishedState(query.state.data.state) ? false : 4_000,
  })
  const current = status.data ?? started
  const reported = useRef(false)
  useEffect(() => {
    if (current?.state !== "Succeeded" || reported.current) return
    reported.current = true
    onDeployed(current)
  }, [current, onDeployed])
  const running = create.isPending || (current !== null && !finishedState(current.state))
  const failed = current !== null && finishedState(current.state) && current.state !== "Succeeded"
  const locked = running || current?.state === "Succeeded"

  const lifecycle = chosen?.lifecycle_status ? LIFECYCLE_LABEL[chosen.lifecycle_status.toLowerCase()] : undefined
  return <Dialog
    open
    wide
    onClose={() => { if (!create.isPending) onClose() }}
    title={t.deployModel}
    description={t.deployModelHint}
    footer={<>
      <Button disabled={create.isPending} onClick={onClose}>{running ? t.close : t.cancel}</Button>
      <Button variant="primary" disabled={!valid || locked} onClick={() => { create.reset(); setStarted(null); create.mutate() }}>
        {running ? <LoaderCircle className="spin" size={13} /> : <Rocket size={13} />}{running ? t.deployingShort : t.deploySubmit}
      </Button>
    </>}
  >
    {accounts.isLoading ? <div className="state"><LoaderCircle className="spin" size={14} />{t.loading}</div>
      : accounts.error ? <ErrorText error={accounts.error} />
      : !accounts.data?.length ? <div className="state warning"><CircleAlert size={14} />{t.deployNoAccounts}</div>
      : <div className="form-grid deploy-form">
        <div className="span-2"><Field label={t.deployAccount}>
          <select value={accountId} disabled={locked} onChange={(event) => setAccountId(event.target.value)}>
            {accounts.data.map((account) => <option key={account.id} value={account.id}>
              {`${account.name} · ${account.location} · ${account.kind}`}
            </option>)}
          </select>
        </Field></div>

        {models.isLoading ? <div className="state span-2"><LoaderCircle className="spin" size={14} />{t.deployLoadingModels}</div>
          : models.error ? <div className="span-2"><ErrorText error={models.error} /></div>
          : <>
            <Field label={t.deployModelName}>
              <input value={filter} disabled={locked} placeholder={t.deployFilterModels} aria-label={t.deployFilterModels} onChange={(event) => setFilter(event.target.value)} />
              <select value={modelName} disabled={locked} size={6} aria-label={t.deployModelName} onChange={(event) => chooseModel(event.target.value)}>
                {names.map((candidate) => <option key={candidate} value={candidate}>{candidate}</option>)}
              </select>
            </Field>
            <Field label={t.deployVersion}>
              <select value={version} disabled={locked || !modelName} onChange={(event) => chooseVersion(event.target.value)}>
                {versionsOf(all, modelName).map((item) => <option key={item.version} value={item.version}>
                  {item.version}{item.is_default_version ? ` (${t.deployDefaultVersion})` : ""}
                </option>)}
              </select>
              {chosen && <small className="deploy-facts">
                {lifecycle && <span className="tag" data-tone="warn">{t[lifecycle]}</span>}
                {chosen.retires_on && <span>{fill(t.deployRetires, { date: new Date(chosen.retires_on).toLocaleDateString(locale === "zh" ? "zh-CN" : "en") })}</span>}
                {chosen.capabilities.length > 0 && <span className="muted">{chosen.capabilities.join(" · ")}</span>}
              </small>}
            </Field>

            {chosen && <div className="span-2 deploy-sku">
              <span className="field-label">{t.deploySku}</span>
              <div className="segment" role="group" aria-label={t.deploySku}>
                {chosen.skus.map((item) => <button key={item.name} type="button" className={sku === item.name ? "active" : ""} aria-pressed={sku === item.name} disabled={locked} onClick={() => chooseSku(item.name)}>
                  {t[SKU_LABEL[item.name].label]}
                </button>)}
              </div>
              {sku && <small className="muted">{t[SKU_LABEL[sku].hint]}</small>}
            </div>}

            {option && <Field label={t.deployCapacity} hint={capacityInfo.data?.error ? fill(t.deployCapacityUnknown, { error: capacityInfo.data.error }) : undefined}>
              <input type="number" min={option.min_capacity} max={limit} step={option.step} value={capacity} disabled={locked || noQuota}
                onChange={(event) => setCapacity(Number(event.target.value))}
                onBlur={() => setCapacity(clampCapacity(capacity, option, available))} />
              <small className={noQuota ? "warn" : "muted"}>
                {capacityInfo.isLoading ? t.deployCapacityChecking
                  : noQuota ? t.deployNoQuota
                  : available !== undefined ? fill(t.deployCapacityAvailable, { count: available.toLocaleString() })
                  : fill(t.deployCapacityRange, { min: option.min_capacity, max: option.max_capacity.toLocaleString() })}
              </small>
            </Field>}
            {option && <Field label={t.deployName} hint={nameTaken ? undefined : t.deployNameHint}>
              <input value={name} disabled={locked} aria-invalid={name !== "" && (!nameValid || nameTaken)} onChange={(event) => { setName(event.target.value); setNameEdited(true) }} />
              {nameTaken && <small className="warn">{t.deployNameTaken}</small>}
            </Field>}
            {option && <div className="span-2"><Field label={t.deployUpgrade}>
              <select value={upgrade} disabled={locked} onChange={(event) => setUpgrade(event.target.value as VersionUpgrade)}>
                {UPGRADES.map((item) => <option key={item.value} value={item.value}>{t[item.label]}</option>)}
              </select>
            </Field></div>}
          </>}
      </div>}

    {current && !failed && <div className={`state ${current.state === "Succeeded" ? "" : "deploy-progress"}`} role="status">
      {current.state === "Succeeded" ? <CheckCircle2 size={14} /> : <LoaderCircle className="spin" size={14} />}
      {current.state === "Succeeded" ? fill(t.deployedNotice, { name: current.deployment }) : fill(t.deploying, { name: current.deployment, state: current.state })}
    </div>}
    {failed && <ErrorText error={fill(t.deployFailed, { error: current.error ?? current.state })} />}
    <ErrorText error={create.error ?? status.error} />
  </Dialog>
}
