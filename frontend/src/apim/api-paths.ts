// The API paths clients call a pool at, and the pool's write payload.
import type { PoolWrite } from "./types.ts"

// Same rule as API_PATH_PATTERN in backend/pool_manager/apim/models.py.
export const API_PATH_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._~-]*(?:\/[A-Za-z0-9._~-]+)*$/

/** A readable API path for a new pool: its title, else its ID without the app's prefix. */
export function suggestApiPath(title: string | null | undefined, poolId: string, prefix: string) {
  const slug = (value: string) => value.trim().toLowerCase()
    .replace(/[^a-z0-9._~-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^[^a-z0-9]+|-+$/g, "")
  const id = prefix && poolId.toLowerCase().startsWith(prefix.toLowerCase()) ? poolId.slice(prefix.length) : poolId
  return slug(title ?? "") || slug(id) || slug(poolId)
}

/** The pool's write payload: never silently drop requested affinity. */
export function poolPayload(draft: PoolWrite, affinitySupported: boolean): PoolWrite {
  if (draft.session_affinity && !affinitySupported) {
    throw new Error("session affinity is not supported by the configured pool API version")
  }
  return { ...draft, session_affinity: affinitySupported && draft.session_affinity === true }
}
