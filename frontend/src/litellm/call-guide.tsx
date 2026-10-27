// "How to call this pool": what a client needs to call the pool through the gateway, with code
// generated for it, and the setup that otherwise makes the first call fail.
import { Check, CircleAlert, Copy, Terminal } from "lucide-react"
import { useState } from "react"

import { callSnippet, KEY_PLACEHOLDER, SNIPPET_LANGUAGES, type SnippetLanguage } from "./call-snippets"
import { fill, useI18n } from "../i18n"
import type { Capabilities, PoolEndpoint } from "./types"
import { KeyStep, TryIt } from "./api-tester"
import { LoadTest } from "./benchmark-panel"
import { baseUrlOf } from "./pool-endpoint"
import { FoundryAccessNotice, useOpenSetup } from "../components/setup"

const LANGUAGE_LABEL: Record<SnippetLanguage, string> = {
  curl: "curl",
  python: "Python",
  javascript: "JavaScript",
  powershell: "PowerShell",
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useI18n("litellm")
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

export function CodeBlock({ code, label }: { code: string; label: string }) {
  return <div className="code-block">
    <pre><code>{code}</code></pre>
    <CopyButton text={code} label={label} />
  </div>
}

export function CallGuide({
  endpoint,
  capabilities,
  canWrite,
  usesIdentity,
  accountIds,
}: {
  endpoint: PoolEndpoint | undefined
  capabilities: Capabilities | undefined
  canWrite: boolean
  /** Some members are Foundry deployments the gateway calls as its managed identity. */
  usesIdentity: boolean
  /** Cognitive Services accounts of the pool's Foundry members. */
  accountIds: string[]
}) {
  const { t, locale } = useI18n("litellm")
  const openSetup = useOpenSetup()
  const [language, setLanguage] = useState<SnippetLanguage>("python")
  const [model, setModel] = useState<string | null>(null)
  // Kept in this page's memory only.
  const [key, setKey] = useState("")
  const [keyInCode, setKeyInCode] = useState(false)

  const header = <header className="section-header"><b className="call-title"><Terminal size={14} />{t.callGuideTitle}</b></header>
  if (!endpoint) return <section className="call-guide">{header}</section>

  const baseUrl = baseUrlOf(endpoint, capabilities?.gateway_url)
  const modelName = model && endpoint.model_names.includes(model) ? model : endpoint.pool_id
  const code = callSnippet(language, { baseUrl, model: modelName, key: keyInCode && key ? key : undefined })
  const principalId = capabilities?.gateway_principal_id ?? null

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

    {endpoint.model_names.length > 1 && <div className="segment" role="group" aria-label={t.modelName}>
      {endpoint.model_names.map((item) => <button key={item} type="button" className={item === modelName ? "active" : ""} aria-pressed={item === modelName} onClick={() => setModel(item)}>{item}</button>)}
    </div>}

    <dl className="call-facts">
      <div><dt>{t.baseUrl}</dt><dd><code>{baseUrl}</code><CopyButton text={baseUrl} label={t.baseUrl} /></dd></div>
      <div><dt>{t.modelName}</dt><dd><code>{modelName}</code><CopyButton text={modelName} label={t.modelName} /></dd></div>
      <div><dt>{t.auth}</dt><dd><code>Authorization: Bearer &lt;key&gt;</code></dd></div>
    </dl>

    <ol className="call-steps">
      <li>
        <b>{t.callStepKey}</b>
        <KeyStep poolId={endpoint.pool_id} canWrite={canWrite} value={key} onChange={setKey} />
      </li>
      <li>
        <b>{t.callStepTry}</b>
        <TryIt poolId={endpoint.pool_id} baseUrl={baseUrl} virtualKey={key} model={modelName} />
      </li>
      <li>
        <b>{t.callStepLoad}</b>
        <LoadTest poolId={endpoint.pool_id} baseUrl={baseUrl} virtualKey={key} model={modelName} canWrite={canWrite} />
      </li>
      <li>
        <b>{t.callStepRequest}</b>
        <p>{fill(t.callStepRequestBody, { placeholder: KEY_PLACEHOLDER }, locale)}</p>
        {key && <label className="check"><input type="checkbox" checked={keyInCode} onChange={(event) => setKeyInCode(event.target.checked)} />{t.keyInCode}</label>}
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
        <li>{t.callNoteRetry}</li>
        <li>{t.callNote429}</li>
        <li>{t.callNoteStream}</li>
        <li>{t.callNoteResponses}</li>
      </ul>
    </details>
  </section>
}
