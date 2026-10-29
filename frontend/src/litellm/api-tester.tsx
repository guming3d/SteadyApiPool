// "How to call" steps that work inside the app: get a LiteLLM virtual key for the pool, then send
// a test request with it. The key stays in this page's memory only: LiteLLM keeps only a hash of
// it, so it is shown once, when it is created.
import { CheckCircle2, CircleAlert, Eye, EyeOff, KeyRound, LoaderCircle, Plus, RefreshCw, Send } from "lucide-react"
import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import { api, keys, queries } from "./api"
import { DEFAULT_TEST_PATH, defaultTestBody, diagnose, parseTestBody, prettyBody, servedBy, type Diagnosis } from "./api-test"
import { fill, useI18n } from "../i18n"
import { isGuest } from "../session"
import type { ApiTestResult } from "../types"
import { CopyButton } from "./call-guide"
import { useOpenSetup } from "../components/setup"
import { Button, ErrorText } from "../components/ui"

/** The guest view creates no keys and sends no requests (the backend refuses both). */
function GuestNotice() {
  const { t } = useI18n("litellm")
  return <div className="state"><KeyRound size={14} />{t.guestNoKeys}</div>
}

type KeyStepProps = {
  poolId: string
  canWrite: boolean
  value: string
  onChange: (key: string) => void
}

/** Step 1: create the pool's test key (shown once), or paste a key the gateway gave out. */
export function KeyStep(props: KeyStepProps) {
  return isGuest() ? <GuestNotice /> : <KeyStepBody {...props} />
}

function KeyStepBody({ poolId, canWrite, value, onChange }: KeyStepProps) {
  const { t } = useI18n("litellm")
  const queryClient = useQueryClient()
  const list = useQuery(queries.poolKeys(poolId, canWrite))
  const [visible, setVisible] = useState(false)
  const [created, setCreated] = useState(false)
  const create = useMutation({
    mutationFn: () => api.createPoolKey(poolId),
    onSuccess: (secret) => {
      onChange(secret.key)
      setVisible(true)
      setCreated(true)
      void queryClient.invalidateQueries({ queryKey: keys.poolKeys(poolId) })
    },
  })
  const testKey = list.data?.find((key) => key.managed)
  const others = list.data?.filter((key) => !key.managed && !key.blocked) ?? []

  return <>
    <p>{t.callStepKeyBody}</p>
    {canWrite && <>
      {list.isLoading && <div className="state"><LoaderCircle className="spin" size={14} />{t.keyLoading}</div>}
      <ErrorText error={list.error} />
      {list.data && <div className="key-options">
        {testKey && <ul className="key-list"><li>
          <KeyRound size={14} />
          <div><b>{testKey.alias}</b><span className="muted">{testKey.key_name ?? ""} · {t.keyScopePool}</span></div>
        </li></ul>}
        {others.length > 0 && <p className="muted">{fill(t.keyOthers, { count: others.length })}</p>}
        <div><Button variant={value ? "outline" : "primary"} disabled={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? <LoaderCircle className="spin" size={13} /> : testKey ? <RefreshCw size={13} /> : <Plus size={13} />}
          {testKey ? t.keyRotate : t.keyCreate}
        </Button></div>
        {testKey && !created && <small className="muted">{t.keyRotateHint}</small>}
        <ErrorText error={create.error} />
      </div>}
    </>}
    {!canWrite && <p className="muted">{t.keyNeedsWrite}</p>}
    {created && <div className="state" data-status="active"><CheckCircle2 size={14} />{t.keyShownOnce}</div>}
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

const DIAGNOSIS_TEXT: Record<Exclude<Diagnosis, "ok" | "other">, "diagKey" | "diagKeyScope" | "diagMemberAccess" | "diagNotFound" | "diagThrottled" | "diagUnavailable" | "diagTimeout"> = {
  key: "diagKey",
  key_scope: "diagKeyScope",
  member_access: "diagMemberAccess",
  not_found: "diagNotFound",
  throttled: "diagThrottled",
  unavailable: "diagUnavailable",
  timeout: "diagTimeout",
}

function Result({ result }: { result: ApiTestResult }) {
  const { t } = useI18n("litellm")
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
        {served.rerouted > 0 && ` ${fill(t.testRerouted, { count: served.rerouted })}`}
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
  virtualKey: string
  model: string
}

/** Step 2: send a request to the pool through the app's backend and explain the answer. */
export function TryIt(props: TryItProps) {
  return isGuest() ? <GuestNotice /> : <TryItBody {...props} />
}

function TryItBody({ poolId, baseUrl, virtualKey, model }: TryItProps) {
  const { t } = useI18n("litellm")
  const [path, setPath] = useState(DEFAULT_TEST_PATH)
  // Follows the chosen model name until the user edits the body.
  const [edited, setEdited] = useState<string | null>(null)
  const text = edited ?? defaultTestBody(model)
  const body = parseTestBody(text)
  const send = useMutation({
    mutationFn: () => api.testPool(poolId, {
      path: path.trim().replace(/^\/+/, ""),
      key: virtualKey || null,
      body: body ?? {},
    }),
  })
  return <>
    <p>{t.testHint}</p>
    <div className="test-form">
      <label className="field">{t.testPath}
        <div className="test-path"><code>{baseUrl.replace(/\/+$/, "")}/</code><input value={path} onChange={(event) => setPath(event.target.value)} spellCheck={false} /></div>
      </label>
      <label className="field">{t.testBody}
        <textarea rows={7} value={text} onChange={(event) => setEdited(event.target.value)} spellCheck={false} aria-invalid={!body} />
        {!body && <small className="warn-text">{t.testBodyInvalid}</small>}
      </label>
    </div>
    {!virtualKey && <p className="warn-text">{t.testNeedsKey}</p>}
    <div><Button variant="primary" disabled={!body || !virtualKey || send.isPending} onClick={() => send.mutate()}>
      {send.isPending ? <LoaderCircle className="spin" size={13} /> : <Send size={13} />}{send.isPending ? t.testSending : t.testSend}
    </Button></div>
    <ErrorText error={send.error} />
    {send.data && <Result result={send.data} />}
  </>
}
