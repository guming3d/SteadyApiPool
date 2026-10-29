// Unsaved pool edits, kept in this page's memory while their editor is closed (another tab or
// pool), so leaving a pool doesn't silently drop what was added to it. A draft comes back only on
// the version of the pool it was made on (its ETag): if the pool changed meanwhile, it is
// dropped rather than published over the change. Pure apart from the browser's leave warning,
// so node --test can import it.
import type { Product } from "./types"

type Kept = { etag: string | null; value: unknown }

const drafts = new Map<string, Kept>()
let warning = false

const draftKey = (product: Product, poolId: string) => `${product}:${poolId.toLowerCase()}`

/** Keeps the pool's unsaved edits (null: there are none), made on the pool at ``etag``. */
export function keepDraft(product: Product, poolId: string, etag: string | null, value: unknown) {
  if (value === null) {
    drafts.delete(draftKey(product, poolId))
    return
  }
  drafts.set(draftKey(product, poolId), { etag, value })
  warnBeforeLeaving()
}

/** Takes the pool's kept edits: ``value`` when made on the pool at ``etag``, ``stale`` when the
 * pool changed since, null when there are none. */
export function takeDraft<T>(product: Product, poolId: string, etag: string | null): { value: T } | "stale" | null {
  const key = draftKey(product, poolId)
  const kept = drafts.get(key)
  drafts.delete(key)
  if (!kept) return null
  return kept.etag === etag ? { value: kept.value as T } : "stale"
}

export function hasDrafts() {
  return drafts.size > 0
}

/** Kept edits are in memory only, so the browser asks before a reload or close drops them. */
function warnBeforeLeaving() {
  if (warning || typeof window === "undefined") return
  warning = true
  window.addEventListener("beforeunload", (event) => {
    if (hasDrafts()) event.preventDefault()
  })
}
