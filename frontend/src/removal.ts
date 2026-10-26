// Removing a pool with its APIs, and cleaning up a service; see components/removal-dialog.tsx.
import type { RemovalConfirm, RemovalPlan } from "./types"

/** Confirms exactly the plan shown: the backend refuses to remove anything not listed. */
export function confirmPlan(plan: RemovalPlan, etag?: string | null): RemovalConfirm {
  return {
    pools: plan.pools,
    delete_apis: plan.delete_apis.map((item) => item.id),
    unbind_apis: plan.unbind_apis.map((item) => item.id),
    ...(etag ? { etag } : {}),
  }
}

/** Nothing this app created is left to remove. */
export function planIsEmpty(plan: RemovalPlan): boolean {
  return !plan.pools.length && !plan.delete_apis.length && !plan.unbind_apis.length && !plan.delete_members.length
}

/** The service name typed again matches, as the backend checks it. */
export function nameMatches(typed: string, name: string): boolean {
  return typed.trim().toLowerCase() === name.toLowerCase()
}
