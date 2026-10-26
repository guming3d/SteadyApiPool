// The public app's front door: what the app will do with the visitor's Azure access, and the
// Microsoft sign-in (src/session.ts). Nothing here calls the backend's API.
import { Clock, ExternalLink, Eye, Languages, LogIn, ShieldCheck, UserRound } from "lucide-react"
import { useState } from "react"

import { fill, useI18n } from "../i18n"
import { enterGuest, guestAvailable, signIn } from "../session"
import { ThemeToggle } from "./theme-toggle"
import { Button, ErrorText } from "./ui"

// Where people can get the app to run on their own machine instead (README "Get started").
const REPOSITORY = "https://github.com/guming3d/apim-backend-pool-manager/tree/main"

export function SignInPage({ error }: { error?: unknown }) {
  const { t, locale, setLocale } = useI18n()
  const [pending, setPending] = useState(false)
  const [failed, setFailed] = useState<unknown>(error)
  const start = () => {
    setPending(true)
    signIn().catch((reason: unknown) => { setPending(false); setFailed(reason) })
  }
  const message = failed instanceof Error ? failed.message : failed ? String(failed) : null
  return <div className="shell">
    <nav className="topbar">
      <div className="brand"><img src="/favicon.svg" alt="" width={20} height={20} /><b>{t.appTitle}</b></div>
      <div className="topbar-end">
        <ThemeToggle />
        <button type="button" className="icon-button" aria-label="Language" title={locale === "zh" ? "English" : "简体中文"} onClick={() => setLocale(locale === "zh" ? "en" : "zh")}><Languages size={15} /></button>
      </div>
    </nav>
    <main className="sign-in-page">
      <section className="card sign-in-card">
        <h1>{t.publicTagline}</h1>
        <p>{t.publicLead}</p>
        <ul className="sign-in-points">
          <li><UserRound size={16} /><span>{t.publicPointYou}</span></li>
          <li><Clock size={16} /><span>{t.publicPointTemporary}</span></li>
          <li><ShieldCheck size={16} /><span>{t.publicPointNothing}</span></li>
        </ul>
        <div className="sign-in-actions">
          <Button variant="primary" disabled={pending} onClick={start}><LogIn size={14} />{t.publicSignIn}</Button>
          {guestAvailable() && <Button disabled={pending} onClick={enterGuest}><Eye size={14} />{t.publicGuest}</Button>}
        </div>
        {guestAvailable() && <p className="muted">{t.publicGuestHint}</p>}
        <p className="muted">{t.publicConsent}</p>
        {message && <ErrorText error={fill(t.publicSignInFailed, { error: message })} />}
      </section>
      <footer className="sign-in-footer muted">
        {t.publicLocal} <a href={REPOSITORY} target="_blank" rel="noreferrer"><ExternalLink size={12} />{t.publicLocalLink}</a>
      </footer>
    </main>
  </div>
}
