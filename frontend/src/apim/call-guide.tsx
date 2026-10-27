// "How to call this pool": what a client needs to call the APIM API bound to the pool, with
// code generated for that API, and the setup that otherwise makes the first call fail.
import { Check, CircleAlert, Copy, Terminal } from "lucide-react"
import { useState } from "react"

import {
  callSnippet,
  DEFAULT_API_VERSION,
  KEY_PLACEHOLDER,
  SNIPPET_LANGUAGES,
  type SnippetLanguage,
} from "./call-snippets"
import { fill, useI18n } from "../i18n"
import type { ApiView, Capabilities } from "./types"
import { endpointUrl } from "./api-bindings"
import { KeyStep, TryIt } from "./api-tester"
import { FoundryAccessNotice, useOpenSetup } from "../components/setup"

const LANGUAGE_LABEL: Record<SnippetLanguage, string> = {
  curl: "curl",
  python: "Python",
  javascript: "JavaScript",
  powershell: "PowerShell",
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useI18n("apim")
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return <button type="button" className="icon-button" title={copied ? t.copied : t.copy} aria-label={`${t.copy} ${label}`} onClick={() => void copy()}>
    {copied ? <Check size={13} /> : <Copy size={13} />}
  </button>
}

function CodeBlock({ code, label }: { code: string; label: string }) {
  return <div className="code-block">
    <pre><code>{code}</code></pre>
    <CopyButton text={code} label={label} />
  </div>
}

export function CallGuide({
  apis,
  capabilities,
  model,
  sessionAffinity,
  accountIds,
}: {
  /** APIs bound to this pool. */
  apis: ApiView[]
  capabilities: Capabilities | undefined
  /** The pool's model as read from Azure, if known. */
  model: string | null
  sessionAffinity: boolean
  /** Cognitive Services accounts of the pool's Foundry members. */
  accountIds: string[]
}) {
  const { t, locale } = useI18n("apim")
  const openSetup = useOpenSetup()
  const [apiId, setApiId] = useState<string | null>(null)
  const [language, setLanguage] = useState<SnippetLanguage>("python")
  const [apiVersion, setApiVersion] = useState(DEFAULT_API_VERSION)
  // Per API, since a key usually opens only its own API. Never stored beyond this page.
  const [keysByApi, setKeysByApi] = useState<Record<string, string>>({})
  const [keyInCode, setKeyInCode] = useState(false)

  const header = <header className="section-header"><b className="call-title"><Terminal size={14} />{t.callGuideTitle}</b></header>
  if (!apis.length) {
    return <section className="call-guide">{header}<div className="state">{t.callGuideNoApi}</div></section>
  }

  const api = apis.find((item) => item.id === apiId) ?? apis[0]!
  const baseUrl = endpointUrl(capabilities?.gateway_url, api.path)
  const keyHeader = api.subscription_required ? api.subscription_key_header : null
  const modelName = model ?? "<model>"
  const key = keysByApi[api.id] ?? ""
  const setKey = (value: string) => setKeysByApi((current) => ({ ...current, [api.id]: value }))
  const code = callSnippet(language, {
    baseUrl, keyHeader, apiVersion: apiVersion.trim() || DEFAULT_API_VERSION, model: modelName,
    key: keyInCode && key ? key : undefined,
  })
  const usesIdentity = Boolean(api.binding?.managed_identity_resource)
  const principalId = capabilities?.apim_principal_id ?? null
  const portalUrl = capabilities ? `https://portal.azure.com/#resource${capabilities.apim_resource_id}/overview` : null

  return <section className="call-guide">
    {header}
    <p className="muted call-hint">{t.callGuideHint}</p>

    {usesIdentity && (!principalId || accountIds.length > 0) && <div className="call-prereq">
      <b>{t.callPrereqTitle}</b>
      {!principalId
        ? <div className="foundry-access" data-tone="warn">
          <CircleAlert size={14} /><span>{t.foundryNoIdentity}</span>
          <button type="button" className="link-button" onClick={openSetup}>{t.setupOpen}</button>
        </div>
        : <FoundryAccessNotice accountIds={accountIds} />}
    </div>}

    {apis.length > 1 && <div className="segment" role="group" aria-label={t.api}>
      {apis.map((item) => <button key={item.id} type="button" className={item.id === api.id ? "active" : ""} aria-pressed={item.id === api.id} onClick={() => setApiId(item.id)}>/{item.path}</button>)}
    </div>}

    <dl className="call-facts">
      <div><dt>{t.baseUrl}</dt><dd><code>{baseUrl}</code><CopyButton text={baseUrl} label={t.baseUrl} /></dd></div>
      <div><dt>{t.auth}</dt><dd>{keyHeader ? <code>{keyHeader}</code> : "—"}</dd></div>
      <div><dt>api-version</dt><dd><input value={apiVersion} onChange={(event) => setApiVersion(event.target.value)} aria-label="api-version" /></dd></div>
    </dl>

    <ol className="call-steps">
      <li>
        <b>{t.callStepKey}</b>
        {keyHeader
          ? <KeyStep key={api.id} api={api} portalUrl={portalUrl} apimName={capabilities?.apim_name ?? "APIM"} value={key} onChange={setKey} />
          : <p>{t.callStepKeyNone}</p>}
      </li>
      <li>
        <b>{t.callStepTry}</b>
        <TryIt key={api.id} api={api} apiVersion={apiVersion} subscriptionKey={key} model={modelName} />
      </li>
      <li>
        <b>{t.callStepRequest}</b>
        <p>{fill(t.callStepRequestBody, { placeholder: KEY_PLACEHOLDER }, locale)}</p>
        {keyHeader && key && <label className="check"><input type="checkbox" checked={keyInCode} onChange={(event) => setKeyInCode(event.target.checked)} />{t.keyInCode}</label>}
        <div className="segment code-tabs" role="tablist">
          {SNIPPET_LANGUAGES.map((candidate) => <button key={candidate} type="button" role="tab" aria-selected={candidate === language} className={candidate === language ? "active" : ""} onClick={() => setLanguage(candidate)}>{LANGUAGE_LABEL[candidate]}</button>)}
        </div>
        <CodeBlock code={code} label={LANGUAGE_LABEL[language]} />
      </li>
    </ol>
    <details className="call-notes">
      <summary>{t.callStepNotes}</summary>
      <ul>
        <li>{fill(t.callNoteModel, { model: modelName }, locale)}</li>
        {api.binding?.retry_on_429 && <li>{t.callNoteRetry}</li>}
        <li>{t.callNote503}</li>
        <li>{t.callNoteStream}</li>
        {sessionAffinity && <li>{t.callNoteAffinity}</li>}
      </ul>
    </details>
  </section>
}
