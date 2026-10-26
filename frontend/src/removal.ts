// Removing a pool with its aliases and test keys, and cleaning up the gateway; see
// components/removal-dialog.tsx.
import type { RemovalConfirm, RemovalPlan } from "./types"

/** Confirms exactly the plan shown: the backend refuses to remove anything not listed. */
export function confirmPlan(plan: RemovalPlan, etag?: string | null): RemovalConfirm {
  return {
    pools: plan.pools,
    delete_aliases: plan.delete_aliases.map((item) => item.name),
    unbind_aliases: plan.unbind_aliases.map((item) => item.name),
    delete_keys: plan.delete_keys.map((item) => item.alias ?? item.id),
    ...(etag ? { etag } : {}),
  }
}

/** Nothing this app created is left to remove. */
export function planIsEmpty(plan: RemovalPlan): boolean {
  return !plan.pools.length && !plan.delete_aliases.length && !plan.unbind_aliases.length && !plan.delete_keys.length
}

/** The gateway name typed again matches, as the backend checks it. */
export function nameMatches(typed: string, name: string): boolean {
  return typed.trim().toLowerCase() === name.toLowerCase()
}
