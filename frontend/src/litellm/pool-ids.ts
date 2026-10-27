// Pool IDs and the other model names clients reach a pool by.

// Same rules as backend/pool_manager/models.py. A pool's ID is also the model name clients send.
export const POOL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,79}$/
export const MODEL_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$/

/** A pool ID for a model: the model name with anything an ID can't hold turned into hyphens. */
export function suggestPoolId(model: string | null | undefined, taken: Iterable<string> = []) {
  const slug = (model ?? "").trim().toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 72) || "pool"
  const used = new Set([...taken].map((id) => id.toLowerCase()))
  if (!used.has(slug)) return slug
  for (let index = 2; ; index++) {
    if (!used.has(`${slug}-${index}`)) return `${slug}-${index}`
  }
}
