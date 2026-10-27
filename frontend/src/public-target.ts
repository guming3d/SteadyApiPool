// The public app, which manages APIM services only: which service this browser chose. It is kept
// per signed-in account in localStorage and sent with every request (pool_manager/sessions.py), so the server keeps
// nothing. IDs only, no credentials. Pure, so node --test can import it.
import type { TargetView } from "./types"

export type ChosenTarget = {
  subscription_id: string
  resource_group: string
  /** The APIM service's name. */
  gateway_name: string
  tenant_id: string | null
}

export const TARGET_HEADER = "X-Pool-Manager-Target"

export const targetStorageKey = (accountId: string) => `ai-gateway-pool:target:${accountId}`

/** `<subscription>/<resource group>/<name>`, each part URL-encoded (resource groups may hold
 *  non-ASCII letters, which a header can't carry as is). */
export function targetHeader(target: ChosenTarget): string {
  return [target.subscription_id, target.resource_group, target.gateway_name].map(encodeURIComponent).join("/")
}

export function targetFromView(view: TargetView): ChosenTarget | null {
  if (!view.subscription_id || !view.resource_group || !view.gateway_name) return null
  return {
    subscription_id: view.subscription_id,
    resource_group: view.resource_group,
    gateway_name: view.gateway_name,
    tenant_id: view.tenant_id,
  }
}

const SERVICE_ID = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.ApiManagement\/service\/([^/]+)$/i

/** A service's ARM ID, e.g. one just created, as the target to remember. */
export function targetFromServiceId(id: string, tenantId: string | null): ChosenTarget | null {
  const match = SERVICE_ID.exec(id)
  if (!match) return null
  return { subscription_id: match[1]!, resource_group: match[2]!, gateway_name: match[3]!, tenant_id: tenantId }
}

/** What localStorage holds, or null when it is missing or not a target. */
export function parseStoredTarget(raw: string | null): ChosenTarget | null {
  if (!raw) return null
  try {
    // Browsers from before both gateways were in one app stored "apim_name".
    const value = JSON.parse(raw) as Partial<ChosenTarget> & { apim_name?: string }
    const text = (item: unknown) => typeof item === "string" && item.length > 0
    const name = text(value.gateway_name) ? value.gateway_name : value.apim_name
    if (!text(value.subscription_id) || !text(value.resource_group) || !text(name)) return null
    return {
      subscription_id: value.subscription_id!,
      resource_group: value.resource_group!,
      gateway_name: name!,
      tenant_id: text(value.tenant_id) ? value.tenant_id! : null,
    }
  } catch {
    return null
  }
}
