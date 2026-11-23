// "How to call" steps that work inside the app: get a runtime API key of the gateway, then send a
// test request with it. Keys are listed, created and read with the user's own Azure rights, and
// every key opens every alias on the gateway, so the app keeps one test key for the gateway. The
// value stays in this page's memory only.
import { CheckCircle2, CircleAlert, Eye, EyeOff, KeyRound, LoaderCircle, Plus, Send, ShieldAlert } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { DEFAULT_TEST_PATH, defaultTestBody, diagnose, parseTestBody, prettyBody, servedBy, TEST_PATHS, type Diagnosis } from "./api-test"
import { fill, useI18n } from "../i18n"
import { isGuest } from "../session"
import type { ApiTestResult } from "../types"
import type { ApiKeyView, TestPath } from "./types"
import { CopyButton } from "./call-guide"
import { useOpenSetup } from "../components/setup"
import { Button, ErrorText } from "../components/ui"

function GuestNotice() {
  const { t } = useI18n("aigateway")
  return <div className="state"><KeyRound size={14} />{t.guestNoKeys}</div>
}

type KeyStepProps = {
  canWrite: boolean
  value: string
  onChange: (key: string) => void
}

/** Step 1: create (or reuse) the gateway's test key and read its value, or paste a key. */
export function KeyStep(props: KeyStepProps) {
  return isGuest() ? <GuestNotice /> : <KeyStepBody {...props} />
}

function KeyStepBody({ canWrite, value, onChange }: KeyStepProps) {
  const { t } = useI18n("aigateway")
  const queryClient = useQueryClient()
  const list = useQuery(queries.apiKeys(canWrite))
  const [visible, setVisible] = useState(false)
  const read = useMutation({
    mutationFn: (key: ApiKeyView) => api.keySecret(key.id),
    onSuccess: (secret) => onChange(secret.primary_key),
  })
  const create = useMutation({
    mutationFn: async () => {
      const key = await api.createTestKey()
      return api.keySecret(key.id)
    },
    onSuccess: (secret) => {
      onChange(secret.primary_key)
      void queryClient.invalidateQueries({ queryKey: keys.apiKeys })
    },
  })
  const testKey = list.data?.find((key) => key.managed)
  const others = list.data?.filter((key) => !key.managed) ?? []
  const busy = read.isPending || create.isPending

  return <>
    <p>{t.callStepKeyBody}</p>
    <div className="callout warning"><ShieldAlert size={15} /><div><span>{t.keyGatewayWide}</span></div></div>
    {canWrite && <>
      {list.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.keyLoading}</div>}
      <ErrorText error={list.error} />
      {list.data && <div className="key-options">
        {list.data.length > 0 && <ul className="key-list">{[...(testKey ? [testKey] : []), ...others].map((key) => <li key={key.id}>
          <KeyRound size={14} />
          <div>
            <b>{key.display_name ?? key.id}</b>
            <span className="muted">{key.id}{key.managed ? ` · ${t.keyTestKey}` : ""}{key.state && key.state !== "active" ? ` · ${key.state}` : ""}</span>
          </div>
          <Button variant="ghost" disabled={busy || (key.state !== null && key.state !== "active")} onClick={() => read.mutate(key)}>
            {read.isPending && read.variables?.id === key.id ? <LoaderCircle className="spin" size={13} /> : <Eye size={13} />}{t.keyUse}
          </Button>
        </li>)}</ul>}
        {!testKey && <div><Button variant={value ? "outline" : "primary"} disabled={busy} onClick={() => create.mutate()}>
          {create.isPending ? <LoaderCircle className="spin" size={13} /> : <Plus size={13} />}{t.keyCreate}
        </Button></div>}
        <ErrorText error={create.error ?? read.error} />
      </div>}
    </>}
    {!canWrite && <p className="muted">{t.keyNeedsWrite}</p>}
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

const DIAGNOSIS_TEXT: Record<Exclude<Diagnosis, "ok" | "other">, "diagKey" | "diagMemberAccess" | "diagNotFound" | "diagThrottled" | "diagUnavailable" | "diagNoRetry" | "diagTimeout"> = {
  key: "diagKey",
  member_access: "diagMemberAccess",
  not_found: "diagNotFound",
  throttled: "diagThrottled",
  unavailable: "diagUnavailable",
  no_retry: "diagNoRetry",
  timeout: "diagTimeout",
}

function Result({ result }: { result: ApiTestResult }) {
  const { t } = useI18n("aigateway")
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
        {served.member ? fill(t.testServed, { member: served.member, model: served.model ?? "—" }) : t.testOk}
      </span></div>
      : diagnosis !== "other" && <div className="callout warning"><CircleAlert size={15} /><div>
        <span>{t[DIAGNOSIS_TEXT[diagnosis]]}</span>
        {diagnosis === "member_access" && <div><button type="button" className="link-button" onClick={openSetup}>{t.setupOpen}</button></div>}
      </div></div>}
    {headers.length > 0 && <details className="test-headers">
      <summary>{fill(t.testHeaders, { count: headers.length })}</summary>
      <dl>{headers.map(([name, value]) => <div key={name}><dt>{name}</dt><dd><code>{value}</code></dd></div>)}</dl>
    </details>}
    <div className="code-block"><pre><code>{prettyBody(result.body) || t.testEmpty}</code></pre><CopyButton text={result.body} label={t.testResponse} /></div>
    {result.truncated && <p className="muted">{t.testTruncated}</p>}
  </div>
}

type TryItProps = {
  poolId: string
  baseUrl: string
  apiKey: string
  model: string
}

/** Step 2: send a request to the pool through the app's backend and explain the answer. */
export function TryIt(props: TryItProps) {
  return isGuest() ? <GuestNotice /> : <TryItBody {...props} />
}

function TryItBody({ poolId, baseUrl, apiKey, model }: TryItProps) {
  const { t } = useI18n("aigateway")
  const [path, setPath] = useState<TestPath>(DEFAULT_TEST_PATH)
  // Follows the path and model until the user edits the body.
  const [edited, setEdited] = useState<string | null>(null)
  const text = edited ?? defaultTestBody(model, path)
  const body = parseTestBody(text)
  const send = useMutation({
    mutationFn: () => api.testPool(poolId, { path, key: apiKey || null, body: body ?? {} }),
  })
  return <>
    <p>{t.testHint}</p>
    <div className="test-form">
      <label className="field">{t.testPath}
        <div className="test-path"><code>{baseUrl.replace(/\/+$/, "")}/</code>
          <select value={path} onChange={(event) => { setPath(event.target.value as TestPath); setEdited(null) }}>
            {TEST_PATHS.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </div>
      </label>
      <label className="field">{t.testBody}
        <textarea rows={7} value={text} onChange={(event) => setEdited(event.target.value)} spellCheck={false} aria-invalid={!body} />
        {!body && <small className="warn-text">{t.testBodyInvalid}</small>}
      </label>
    </div>
    {!apiKey && <p className="warn-text">{t.testNeedsKey}</p>}
    <div><Button variant="primary" disabled={!body || !apiKey || send.isPending} onClick={() => send.mutate()}>
      {send.isPending ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{send.isPending ? t.testSending : t.testSend}
    </Button></div>
    <ErrorText error={send.error} />
    {send.data && <Result result={send.data} />}
  </>
}
