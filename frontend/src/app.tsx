import { ChevronDown, Compass, Languages, LogIn, LogOut } from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import { queries } from "./api"
import { DeploymentsPage } from "./components/deployments-page"
import { GatewayMonitor } from "./components/gateway-monitor"
import { PoolsPage } from "./components/pools-page"
import { ProductTour } from "./components/product-tour"
import { SetupButton, SetupProvider, SetupWizard, SwitchGatewayDialog } from "./components/setup-wizard"
import { StartupProgress } from "./components/startup-progress"
import { ThemeToggle } from "./components/theme-toggle"
import { ErrorText } from "./components/ui"
import { useI18n } from "./i18n"
import { SETUP_SEEN_KEY } from "./gateway-switch"
import { displayUser } from "./identity"
import { ProductProvider, useProductUi, type PoolAddRequest, type PoolCreateRequest } from "./product"
import { PRODUCTS } from "./products"
import { isGuest, isPublic, leaveGuest, signOut } from "./session"
import { shouldAutoStart, TOUR_STORAGE_KEY, TOUR_VERSION } from "./tour-steps"

// Members are managed inside their pool; an old ?tab=backends link lands on Pools.
type Tab = "pools" | "deployments" | "gateway"
const TABS: Tab[] = ["pools", "deployments", "gateway"]

export function App() {
  const me = useQuery(queries.me())
  const setup = useQuery(queries.setup())
  // The chosen gateway's kind decides the pages below the top bar and the wording.
  const product = setup.data?.product ?? null
  const ui = product ? PRODUCTS[product] : null
  const { t, locale, setLocale } = useI18n(product)
  // Locally the name comes from the setup report, so wait for it rather than flash "no sign-in".
  const user = me.data && (me.data.authenticated || setup.data) ? displayUser(me.data, setup.data?.user) : undefined
  const [setupOpen, setSetupOpen] = useState(false)
  const [serviceOpen, setServiceOpen] = useState(false)
  // The gateway panel's Switch gateway: the setup step's picker on its own.
  const [switchOpen, setSwitchOpen] = useState(false)
  const openSetup = useCallback(() => setSetupOpen(true), [])
  const autoOpened = useRef(false)
  // Set together with setSetupOpen(true), so the tour can tell in the same render that setup
  // is about to open and waits for it.
  const setupAuto = useRef(false)
  const wasBlocked = useRef(false)
  useEffect(() => {
    // A gateway switch checks the new gateway from scratch (gatewaySwitched): open setup again
    // if that one needs attention.
    if (!setup.data) {
      autoOpened.current = false
      return
    }
    if (!setup.data.can_operate) {
      wasBlocked.current = true
      return
    }
    if (autoOpened.current) return
    autoOpened.current = true
    // Someone who just got past the full-page setup continues with the remaining steps.
    if (!setup.data.ready && (wasBlocked.current || !sessionStorage.getItem(SETUP_SEEN_KEY))) {
      setupAuto.current = true
      setSetupOpen(true)
    }
  }, [setup.data])
  const closeSetup = () => {
    sessionStorage.setItem(SETUP_SEEN_KEY, "1")
    setupAuto.current = false
    setSetupOpen(false)
  }
  // The product tour opens once, after setup lets the app work; Tour in the top bar replays it.
  const [tourOpen, setTourOpen] = useState(false)
  const tourAsked = useRef(false)
  useEffect(() => {
    if (tourAsked.current || !setup.data || !autoOpened.current || setupAuto.current) return
    if (shouldAutoStart(localStorage.getItem(TOUR_STORAGE_KEY), setup.data.can_operate, setupOpen)) {
      tourAsked.current = true
      setTourOpen(true)
    }
  }, [setup.data, setupOpen])
  const closeTour = () => {
    localStorage.setItem(TOUR_STORAGE_KEY, TOUR_VERSION)
    setTourOpen(false)
  }
  // Nothing else works until the app can reach the gateway, so setup takes the whole page.
  const blocked = setup.data?.can_operate === false || (setup.data !== undefined && !ui)
  const [tab, setTab] = useState<Tab>(() => {
    const requested = new URLSearchParams(window.location.search).get("tab") as Tab | null
    return requested && TABS.includes(requested) ? requested : "pools"
  })
  const [createRequest, setCreateRequest] = useState<PoolCreateRequest | null>(null)
  const clearCreateRequest = useCallback(() => setCreateRequest(null), [])
  const [addRequest, setAddRequest] = useState<PoolAddRequest | null>(null)
  const clearAddRequest = useCallback(() => setAddRequest(null), [])
  const selectTab = (next: Tab) => {
    const url = new URL(window.location.href)
    url.searchParams.set("tab", next)
    window.history.replaceState(null, "", url)
    setTab(next)
  }

  return <SetupProvider open={openSetup}><div className="shell">
    <nav className="topbar">
      <div className="brand"><img src="/favicon.svg" alt="" width={20} height={20} /><b>{t.appTitle}</b>
        {ui && !blocked && <ProductProvider key={ui.product} ui={ui}><GatewayButton onOpen={() => setServiceOpen(true)} /></ProductProvider>}
      </div>
      <div className="tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "pools"} className={tab === "pools" ? "active" : ""} data-tour="tab-pools" onClick={() => selectTab("pools")}>{t.pools}</button>
        <button type="button" role="tab" aria-selected={tab === "deployments"} className={tab === "deployments" ? "active" : ""} data-tour="tab-deployments" onClick={() => selectTab("deployments")}>{t.deployments}</button>
        <button type="button" role="tab" aria-selected={tab === "gateway"} className={tab === "gateway" ? "active" : ""} data-tour="tab-gateway" onClick={() => selectTab("gateway")}>{t.tabGateway}</button>
      </div>
      <div className="topbar-end">
        {!blocked && setup.data && <button type="button" className="tour-button" title={t.tourButtonHint} onClick={() => setTourOpen(true)}>
          <Compass size={14} />{t.tourButton}
        </button>}
        <SetupButton onOpen={openSetup} />
        {user && <span className="muted" title={user.kind === "user" ? t.signedInAs : user.kind === "local" ? t.localAccountHint : user.kind === "guest" ? t.guestHint : t.localNoSignInHint}>
          {user.kind === "local-none" ? t.localNoSignIn : user.kind === "guest" ? t.guestUser : user.name}{!me.data?.can_write && ` · ${t.readOnly}`}
        </span>}
        {isGuest()
          ? <button type="button" className="tour-button" title={t.guestSignInHint} onClick={leaveGuest}><LogIn size={14} />{t.publicSignIn}</button>
          : isPublic() && <button type="button" className="tour-button" title={t.publicSignOutHint} onClick={() => void signOut()}>
            <LogOut size={14} />{t.publicSignOut}
          </button>}
        <ThemeToggle />
        <button type="button" className="icon-button" aria-label="Language" title={locale === "zh" ? "English" : "简体中文"} onClick={() => setLocale(locale === "zh" ? "en" : "zh")}><Languages size={15} /></button>
      </div>
    </nav>
    <main>
      {setup.isLoading ? <StartupProgress />
        : blocked ? <div className="page"><SetupWizard page /></div>
        : me.error ? <ErrorText error={me.error} />
        : ui && <ProductProvider key={ui.product} ui={ui}>
          {tab === "pools" ? <PoolsPage createRequest={createRequest} onCreateRequestHandled={clearCreateRequest} addRequest={addRequest} onAddRequestHandled={clearAddRequest} />
            : tab === "gateway" ? <GatewayMonitor />
            : <DeploymentsPage
              onCreatePool={(request) => { setCreateRequest(request); selectTab("pools") }}
              onAddToPool={(request) => { setAddRequest(request); selectTab("pools") }}
            />}
        </ProductProvider>}
    </main>
    {setupOpen && !blocked && <SetupWizard onClose={closeSetup} />}
    {serviceOpen && !blocked && ui && <ProductProvider key={ui.product} ui={ui}>
      <ui.ServicePanel onClose={() => setServiceOpen(false)} onSwitch={() => { setServiceOpen(false); setSwitchOpen(true) }} />
    </ProductProvider>}
    {switchOpen && !blocked && <SwitchGatewayDialog onClose={() => setSwitchOpen(false)} />}
    {tourOpen && !blocked && !setupOpen && <ProductTour product={product} onClose={closeTour} />}
  </div></SetupProvider>
}

/** The chosen gateway's name in the top bar; it opens the gateway panel. */
function GatewayButton({ onOpen }: { onOpen: () => void }) {
  const { product, useFacts } = useProductUi()
  const { t } = useI18n(product)
  const facts = useFacts()
  if (!facts) return null
  return <button type="button" className="brand-service" data-tour="gateway" title={t.serviceOpen} aria-haspopup="dialog" onClick={onOpen}>
    <code>{facts.name}</code><ChevronDown size={12} />
  </button>
}
