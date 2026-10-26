// "How to call" steps that work inside the app: get a subscription key for the API, then send a
// test request through it. The key stays in this page's memory only.
import { CheckCircle2, CircleAlert, ExternalLink, Eye, EyeOff, KeyRound, LoaderCircle, Plus, Send } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, ApiError, keys, queries } from "../api"
import { DEFAULT_TEST_PATH, defaultTestBody, diagnose, parseTestBody, prettyBody, servedBy, type Diagnosis } from "../api-test"
import { fill, useI18n } from "../i18n"
import { isGuest } from "../session"
import type { ApiTestResult, ApiView, SubscriptionKeyView } from "../types"
import { CopyButton } from "./call-guide"
import { useOpenSetup } from "./setup-wizard"
import { Button, ErrorText } from "./ui"

type Messages = ReturnType<typeof useI18n>["t"]

function scopeText(key: SubscriptionKeyView, t: Messages) {
  if (key.scope === "api") return t.keyScopeApi
  if (key.scope === "product") return fill(t.keyScopeProduct, { product: key.product ?? "" })
  return t.keyScopeAll
}

/** Step 1: pick an existing subscription, create a test one, or paste a key from the portal. */
type KeyStepProps = {
  api: ApiView
  portalUrl: string | null
  apimName: string
  value: string
  onChange: (key: string) => void
}

/** The guest view reads no keys and sends no requests (the backend refuses both). */
function GuestNotice() {
  const { t } = useI18n()
  return <div className="state"><KeyRound size={14} />{t.guestNoKeys}</div>
}

export function KeyStep(props: KeyStepProps) {
  return isGuest() ? <GuestNotice /> : <KeyStepBody {...props} />
}

function KeyStepBody({ api: target, portalUrl, apimName, value, onChange }: KeyStepProps) {
  const { t, locale } = useI18n()
  const queryClient = useQueryClient()
  const list = useQuery(queries.apiKeys(target.id))
  const [visible, setVisible] = useState(false)
  const read = useMutation({
    mutationFn: (keyId: string) => api.readApiKey(target.id, keyId),
    onSuccess: (secret) => onChange(secret.primary_key),
  })
  const create = useMutation({
    mutationFn: async () => {
      const created = await api.createApiKey(target.id)
      onChange((await api.readApiKey(target.id, created.id)).primary_key)
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.apiKeys(target.id) }),
  })
  const usable = list.data?.filter((key) => key.state === "active") ?? []
  const hasTestKey = list.data?.some((key) => key.managed) ?? false
  // Without rights to read keys (or, deployed, without delegated access) the portal is the way.
  const blocked = list.error instanceof ApiError && [401, 403].includes(list.error.status)

  return <>
    <p>{fill(t.callStepKeyBody, { header: target.subscription_key_header }, locale)}</p>
    {list.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.keyLoading}</div>}
    {blocked
      ? <div className="callout warning"><CircleAlert size={15} /><div><span>{t.keyCannotRead}</span><small className="muted">{list.error?.message}</small></div></div>
      : <ErrorText error={list.error} />}
    {list.data && <div className="key-options">
      {usable.length > 0 && <ul className="key-list">{usable.map((key) => <li key={key.id}>
        <KeyRound size={14} />
        <div><b>{key.display_name}</b><span className="muted">{scopeText(key, t)}{key.scope === "all_apis" && ` · ${t.keyScopeAllWarn}`}</span></div>
        <Button disabled={read.isPending} onClick={() => read.mutate(key.id)}>
          {read.isPending && read.variables === key.id && <LoaderCircle className="spin" size={13} />}{t.keyUse}
        </Button>
      </li>)}</ul>}
      {!usable.length && <p>{t.keyNone}</p>}
      {!hasTestKey && <div><Button variant={usable.length ? "outline" : "primary"} disabled={create.isPending} onClick={() => create.mutate()}>
        {create.isPending ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{fill(t.keyCreate, { api: target.display_name })}
      </Button></div>}
      <ErrorText error={read.error ?? create.error} />
    </div>}
    <details className="key-portal" open={blocked || undefined}>
      <summary>{t.keyPortalTitle}</summary>
      <ol>
        <li>{fill(t.keyPortal1, { apim: apimName })} {portalUrl && <a href={portalUrl} target="_blank" rel="noreferrer"><ExternalLink size={12} />{t.openPortal}</a>}</li>
        <li>{t.keyPortal2}</li>
        <li>{fill(t.keyPortal3, { api: target.display_name })}</li>
        <li>{t.keyPortal4}</li>
        <li>{t.keyPortal5}</li>
      </ol>
    </details>
    <label className="field key-field">{t.keyLabel}
      <div className="key-input">
        <input type={visible ? "text" : "password"} value={value} placeholder={t.keyPlaceholder} autoComplete="off" spellCheck={false} onChange={(event) => onChange(event.target.value.trim())} />
        <button type="button" className="icon-button" aria-label={visible ? t.keyHide : t.keyShow} title={visible ? t.keyHide : t.keyShow} onClick={() => setVisible(!visible)}>
          {visible ? <EyeOff size={13} /> : <Eye size={13} />}
        </button>
        {value && <CopyButton text={value} label={t.keyLabel} />}
      </div>
      <small>{value ? t.keyReady : t.keyKeptHere}</small>
    </label>
  </>
}

const DIAGNOSIS_TEXT: Record<Exclude<Diagnosis, "ok" | "other">, "diagKey" | "diagMemberAccess" | "diagNotFound" | "diagThrottled" | "diagNoIdentity" | "diagUnavailable" | "diagTimeout"> = {
  key: "diagKey",
  member_access: "diagMemberAccess",
  not_found: "diagNotFound",
  throttled: "diagThrottled",
  no_identity: "diagNoIdentity",
  unavailable: "diagUnavailable",
  timeout: "diagTimeout",
}

function Result({ result }: { result: ApiTestResult }) {
  const { t } = useI18n()
  const openSetup = useOpenSetup()
  const diagnosis = diagnose(result)
  const served = servedBy(result)
  const ok = diagnosis === "ok"
  const headers = Object.entries(result.headers)
  return <div className="test-result" data-ok={ok}>
    <div className="test-result-head">
      <span className="tag" data-tone={ok ? "ok" : "warn"}>{result.status}</span>
      <span className="muted">{fill(t.testElapsed, { ms: result.elapsed_ms })}</span>
      <code title={result.url}>{result.url}</code>
    </div>
    {ok
      ? <div className="foundry-access" data-tone="ok"><CheckCircle2 size={14} /><span>
        {served.model || served.region ? fill(t.testServed, { model: served.model ?? "—", region: served.region ?? "—" }) : t.testOk}
      </span></div>
      : diagnosis !== "other" && <div className="callout warning"><CircleAlert size={15} /><div>
        <span>{t[DIAGNOSIS_TEXT[diagnosis]]}</span>
        {(diagnosis === "member_access" || diagnosis === "no_identity") && <div><button type="button" className="link-button" onClick={openSetup}>{t.setupOpen}</button></div>}
      </div></div>}
    {headers.length > 0 && <details className="test-headers">
      <summary>{fill(t.testHeaders, { count: headers.length })}</summary>
      <dl>{headers.map(([name, value]) => <div key={name}><dt>{name}</dt><dd><code>{value}</code></dd></div>)}</dl>
    </details>}
    <div className="code-block"><pre><code>{prettyBody(result.body) || t.testEmpty}</code></pre><CopyButton text={result.body} label={t.testResponse} /></div>
    {result.truncated && <p className="muted">{t.testTruncated}</p>}
  </div>
}

/** Step 2: send a request to the API through the app's backend and explain the answer. */
type TryItProps = {
  api: ApiView
  apiVersion: string
  subscriptionKey: string
  model: string
}

export function TryIt(props: TryItProps) {
  return isGuest() ? <GuestNotice /> : <TryItBody {...props} />
}

function TryItBody({ api: target, apiVersion, subscriptionKey, model }: TryItProps) {
  const { t } = useI18n()
  const [path, setPath] = useState(DEFAULT_TEST_PATH)
  // Follows the pool's model (read from Azure after the first render) until the user edits it.
  const [edited, setEdited] = useState<string | null>(null)
  const text = edited ?? defaultTestBody(model)
  const setText = (value: string) => setEdited(value)
  const body = parseTestBody(text)
  const send = useMutation({
    mutationFn: () => api.testApi(target.id, {
      path: path.trim().replace(/^\/+/, ""),
      api_version: apiVersion.trim() || null,
      subscription_key: subscriptionKey || null,
      body: body ?? {},
    }),
  })
  const needsKey = target.subscription_required && !subscriptionKey
  return <>
    <p>{t.testHint}</p>
    <div className="test-form">
      <label className="field">{t.testPath}
        <div className="test-path"><code>/{target.path.replace(/^\/+|\/+$/g, "")}/</code><input value={path} onChange={(event) => setPath(event.target.value)} spellCheck={false} /></div>
      </label>
      <label className="field">{t.testBody}
        <textarea rows={7} value={text} onChange={(event) => setText(event.target.value)} spellCheck={false} aria-invalid={!body} />
        {!body && <small className="warn-text">{t.testBodyInvalid}</small>}
      </label>
    </div>
    {needsKey && <p className="warn-text">{t.testNeedsKey}</p>}
    <div><Button variant="primary" disabled={!body || send.isPending} onClick={() => send.mutate()}>
      {send.isPending ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{send.isPending ? t.testSending : t.testSend}
    </Button></div>
    <ErrorText error={send.error} />
    {send.data && <Result result={send.data} />}
  </>
}
