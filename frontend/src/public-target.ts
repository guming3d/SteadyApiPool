// The public app (not available with the LiteLLM gateway yet): which gateway this browser chose. It is kept per signed-in account in
// localStorage and sent with every request (pool_manager/sessions.py), so the server keeps
// nothing. IDs only, no credentials. Pure, so node --test can import it.
import type { TargetView } from "./types"

export type ChosenTarget = {
  subscription_id: string
  resource_group: string
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

const SERVICE_ID = /^\/subscriptions\/([^/]+)\/resourceGroups\/([^/]+)\/providers\/Microsoft\.App\/containerApps\/([^/]+)$/i

/** A gateway's ARM ID (its Container App), e.g. one just deployed, as the target to remember. */
export function targetFromServiceId(id: string, tenantId: string | null): ChosenTarget | null {
  const match = SERVICE_ID.exec(id)
  if (!match) return null
  return { subscription_id: match[1]!, resource_group: match[2]!, gateway_name: match[3]!, tenant_id: tenantId }
}

/** What localStorage holds, or null when it is missing or not a target. */
export function parseStoredTarget(raw: string | null): ChosenTarget | null {
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<ChosenTarget>
    const text = (item: unknown) => typeof item === "string" && item.length > 0
    if (!text(value.subscription_id) || !text(value.resource_group) || !text(value.gateway_name)) return null
    return {
      subscription_id: value.subscription_id!,
      resource_group: value.resource_group!,
      gateway_name: value.gateway_name!,
      tenant_id: text(value.tenant_id) ? value.tenant_id! : null,
    }
  } catch {
    return null
  }
}
