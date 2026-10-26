// The public app's sign-in (pool_manager/entra.py): MSAL.js in the browser, with tokens for this
// app's own API only. The backend exchanges each one for an Azure token as the user and keeps
// nothing, so signing out here and in the backend ends all access. Locally and in the Easy Auth
// deployment none of this runs, and MSAL isn't even loaded.
import type { AccountInfo, PublicClientApplication } from "@azure/msal-browser"

import { configureRequests } from "./api"
import { parseStoredTarget, TARGET_HEADER, targetHeader, targetStorageKey, type ChosenTarget } from "./public-target"
import type { PublicConfig } from "./types"

let config: PublicConfig = { mode: "local", client_id: null, authority: null, api_scope: null, consent_scopes: [], guest: false }
let msal: PublicClientApplication | null = null
let account: AccountInfo | null = null
let interactionRequired: (error: unknown) => boolean = () => false

// The MSAL redirect bridge (src/redirect-bridge.ts); every flow returns through it.
const redirectUri = () => `${window.location.origin}/redirect.html`
const home = () => `${window.location.origin}/`

// The read-only guest view lasts as long as this tab, until the visitor signs in.
const GUEST_KEY = "ai-gateway-pool:guest"
let guest = false

export const isPublic = () => config.mode === "public"
export const signedIn = () => account !== null
export const isGuest = () => guest
export const guestAvailable = () => config.guest

/** Browses the deployment's demo service read-only, without signing in (pool_manager/sessions.py). */
export function enterGuest() {
  sessionStorage.setItem(GUEST_KEY, "1")
  window.location.assign(home())
}

/** Leaves the guest view for the sign-in page. */
export function leaveGuest() {
  sessionStorage.removeItem(GUEST_KEY)
  window.location.assign(home())
}

/** Reads how this backend signs people in and, in the public app, finishes a sign-in that
 *  just came back from Entra. */
export async function startSession(): Promise<void> {
  const response = await fetch("/api/config", { cache: "no-store" })
  // The rest of the app reports a backend that doesn't answer.
  if (!response.ok) return
  config = await response.json() as PublicConfig
  if (!isPublic()) return
  const { BrowserCacheLocation, InteractionRequiredAuthError, PublicClientApplication } = await import("@azure/msal-browser")
  interactionRequired = (error) => error instanceof InteractionRequiredAuthError
  msal = new PublicClientApplication({
    auth: { clientId: config.client_id!, authority: config.authority!, redirectUri: redirectUri(), postLogoutRedirectUri: home() },
    // Tokens last as long as this tab, never across browser restarts.
    cache: { cacheLocation: BrowserCacheLocation.SessionStorage },
  })
  await msal.initialize()
  const result = await msal.handleRedirectPromise()
  account = result?.account ?? msal.getActiveAccount() ?? msal.getAllAccounts()[0] ?? null
  if (account) {
    msal.setActiveAccount(account)
    sessionStorage.removeItem(GUEST_KEY)
  }
  guest = !account && config.guest && sessionStorage.getItem(GUEST_KEY) === "1"
  configureRequests(requestHeaders)
}

function authorityFor(tenantId: string | null | undefined) {
  // config.authority ends in /organizations: any work or school account.
  return tenantId ? config.authority!.replace(/\/[^/]+$/, `/${tenantId}`) : config.authority!
}

/** Full-page sign-in with Entra, into a given tenant if asked; it returns to this page. The
 *  first time, Entra asks the user (or their admin) to consent to Azure access as them.
 *
 *  It asks for no scope of this app's own API: in a tenant where nobody has used the app yet,
 *  Entra can't resolve `api://<client id>` (AADSTS500011), because the app's service principal
 *  only appears there once someone consents. Consenting to this sign-in creates it; the first
 *  API token (apiToken) follows, with its own consent prompt if the tenant still needs one. */
export function signIn(options: { tenantId?: string | null; consent?: boolean } = {}): Promise<void> {
  return msal!.loginRedirect({
    scopes: ["openid", "profile", "offline_access"],
    // What the backend exchanges the token for (Azure Resource Manager), consented up front.
    extraScopesToConsent: config.consent_scopes,
    authority: authorityFor(options.tenantId),
    prompt: options.consent ? "consent" : "select_account",
    redirectUri: redirectUri(),
  })
}

async function apiToken(current: AccountInfo): Promise<string> {
  const request = { scopes: [config.api_scope!], account: current, authority: authorityFor(current.tenantId) }
  try {
    return (await msal!.acquireTokenSilent(request)).accessToken
  } catch (error) {
    if (!interactionRequired(error)) throw error
    await msal!.acquireTokenRedirect({ ...request, redirectUri: redirectUri() })
    // The page is on its way to Entra.
    return new Promise(() => {})
  }
}

async function requestHeaders(): Promise<Record<string, string>> {
  // Guests send no token at all; the backend answers with the app's read-only view.
  if (guest) return { "X-Pool-Manager-Guest": "1" }
  if (!account) return {}
  const target = chosenTarget()
  return {
    Authorization: `Bearer ${await apiToken(account)}`,
    ...(target ? { [TARGET_HEADER]: targetHeader(target) } : {}),
  }
}

export function chosenTarget(): ChosenTarget | null {
  return account ? parseStoredTarget(localStorage.getItem(targetStorageKey(account.homeAccountId))) : null
}

/** Remembers (or forgets) the gateway this account works on, in this browser only. */
export function chooseTarget(target: ChosenTarget | null) {
  if (!isPublic() || !account) return
  const key = targetStorageKey(account.homeAccountId)
  if (target) localStorage.setItem(key, JSON.stringify(target))
  else localStorage.removeItem(key)
}

/** Ends the backend session (its in-memory Azure tokens), forgets the chosen service, then signs
 *  out of Entra. */
export async function signOut(): Promise<void> {
  try {
    await fetch("/api/session/sign-out", { method: "POST", headers: { ...await requestHeaders(), "X-Pool-Manager": "1" } })
  } catch {
    // The backend forgets idle sessions by itself; signing out of Entra still matters.
  }
  chooseTarget(null)
  await msal!.logoutRedirect({ account: account ?? undefined, postLogoutRedirectUri: home() })
}
