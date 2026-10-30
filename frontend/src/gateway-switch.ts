// What the browser keeps when the gateway changes (chosen, created, connected, disconnected or
// deleted). Pure, so node --test can import it; api.ts's gatewaySwitched applies it.

/** Opened automatically once per browser session; a switch opens it again if the new gateway
 *  needs attention (app.tsx). */
export const SETUP_SEEN_KEY = "ai-gateway-pool:setup-seen"

/** Queries that don't depend on the gateway, kept: who is signed in, the tenants and
 *  subscriptions the pickers list, and the target and setup report themselves, which are
 *  refetched rather than dropped so the components watching them follow the new gateway. Every
 *  other query (pools, members, deployments, metrics, cost, the pickers' gateway lists) was read
 *  from or for the old gateway. */
export function keptAcrossSwitch(queryKey: readonly unknown[]): boolean {
  const [first, second] = queryKey
  if (first === "me" || first === "setup-progress") return true
  if (first === "setup") return queryKey.length === 1
  if (first === "target") return queryKey.length === 1 || second === "tenants" || second === "subscriptions"
  return false
}
