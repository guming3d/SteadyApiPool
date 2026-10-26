// The choices in the deploy-a-model dialog. Pure, so node --test can import it;
// tui/pool_tui/logic.py ports it. The server checks every choice again before deploying.
import type { DeployableModel, ModelSkuOption } from "./types"

/** Same as DEPLOYMENT_NAME_PATTERN in backend/pool_manager/models.py. */
export const DEPLOYMENT_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/
const NAME_MAX = 64

/** Model names in the order the account lists them. */
export function modelNames(models: DeployableModel[]) {
  return [...new Set(models.map((model) => model.name))]
}

/** One model's versions, the default first (the server sorts them that way). */
export function versionsOf(models: DeployableModel[], name: string) {
  return models.filter((model) => model.name === name)
}

/** The version to preselect: the default one, else the first listed. */
export function defaultVersion(models: DeployableModel[], name: string): DeployableModel | undefined {
  const versions = versionsOf(models, name)
  return versions.find((model) => model.is_default_version) ?? versions[0]
}

/** A free deployment name: the model's own, else model-2, model-3, ... (64 characters at most). */
export function suggestDeploymentName(model: string, taken: string[]) {
  const base = model.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, NAME_MAX) || "deployment"
  const used = new Set(taken.map((name) => name.toLowerCase()))
  if (!used.has(base.toLowerCase())) return base
  for (let index = 2; ; index++) {
    const suffix = `-${index}`
    const candidate = `${base.slice(0, NAME_MAX - suffix.length)}${suffix}`
    if (!used.has(candidate.toLowerCase())) return candidate
  }
}

/** The most a deployment may ask for: the type's maximum, capped by the quota left when known. */
export function capacityLimit(sku: ModelSkuOption, available: number | undefined) {
  return available === undefined ? sku.max_capacity : Math.min(sku.max_capacity, available)
}

/** A capacity that fits the type's range and step, and the quota left. */
export function clampCapacity(value: number, sku: ModelSkuOption, available?: number) {
  const limit = capacityLimit(sku, available)
  if (limit < sku.min_capacity) return sku.min_capacity
  const stepped = Math.round((Number.isFinite(value) ? value : sku.default_capacity) / sku.step) * sku.step
  return Math.min(Math.max(stepped, sku.min_capacity), limit)
}

/** Azure's final provisioning states; polling stops at any of them. */
export const finishedState = (state: string) => ["succeeded", "failed", "canceled"].includes(state.toLowerCase())
