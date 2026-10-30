// The command the gateway panel offers for reading a Container Apps gateway's master key. The
// app never sends the key to the browser; the user reads it with their own Azure rights, e.g. to
// sign in to LiteLLM's admin UI.

// The Container App secret holding it, as gateway.MASTER_KEY_SECRET reads it.
const MASTER_KEY_SECRET = "litellm-master-key"

/** Subscription, resource group and name of a Container App's resource ID; null for anything else. */
export function parseContainerAppId(id: string): { subscription: string; group: string; name: string } | null {
  const match = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.App\/containerApps\/([^/]+)$/i.exec(id)
  return match ? { subscription: match[1]!, group: match[2]!, name: match[3]! } : null
}

/** `az` printing the gateway's master key, or null when the ID isn't a Container App's. */
export function masterKeyCommand(appId: string): string | null {
  const app = parseContainerAppId(appId)
  if (!app) return null
  return `az containerapp secret show -g ${app.group} -n ${app.name} --subscription ${app.subscription} --secret-name ${MASTER_KEY_SECRET} --query value -o tsv`
}

export function containerAppPortalUrl(appId: string): string {
  return `https://portal.azure.com/#resource${appId}/overview`
}
