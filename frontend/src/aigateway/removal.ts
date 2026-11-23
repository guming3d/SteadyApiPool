// Removing a pool with its unused members, and cleaning up the gateway; see removal-dialog.tsx.
// Pure, so node --test can import it.
import type { RemovalConfirm, RemovalPlan } from "./types"

/** Confirms exactly the plan shown: the backend refuses to remove anything not listed. */
export function confirmPlan(plan: RemovalPlan, etag?: string | null): RemovalConfirm {
  return {
    pools: plan.pools,
    delete_keys: plan.delete_keys,
    ...(etag ? { etag } : {}),
  }
}

/** Nothing this app created is left to remove. */
export function planIsEmpty(plan: RemovalPlan): boolean {
  return !plan.pools.length && !plan.delete_members.length && !plan.delete_keys.length
}

/** The gateway name typed again matches, as the backend checks it. */
export function nameMatches(typed: string, name: string): boolean {
  return typed.trim().toLowerCase() === name.toLowerCase()
}
