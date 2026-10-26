// Pool modes (failover, balanced, weighted) derived from member priority and weight. The
// operator picks the primary, and it is kept as the first member.
import type { PoolMember, PoolWrite } from "./types"

export type DeploymentMode = "failover" | "balanced" | "weighted" | "custom"

export const MIN_MEMBERS = 2
export const MAX_MEMBERS = 30

/** Lowest priority first. The sort is stable, so equal priorities keep the order this app
 *  wrote and the operator's primary survives a reload in balanced/weighted mode. */
export function primaryFirst(members: PoolMember[]) {
  return [...members].sort((left, right) => left.priority - right.priority)
}

export function primaryId(members: PoolMember[]) {
  return members[0]?.backend_id ?? ""
}

export function deploymentMode(members: PoolMember[]): DeploymentMode {
  const primary = members[0]
  const replicas = members.slice(1)
  if (!primary || !replicas.length) return "failover"
  const equalWeights = new Set(members.map((member) => member.weight)).size === 1
  if (new Set(members.map((member) => member.priority)).size === 1) {
    return equalWeights ? "balanced" : "weighted"
  }
  if (
    equalWeights
    && replicas.every((member) => member.priority > primary.priority)
    && new Set(replicas.map((member) => member.priority)).size === 1
  ) return "failover"
  return "custom"
}

export function membersForMode(
  members: PoolMember[],
  mode: Exclude<DeploymentMode, "custom">,
): PoolMember[] {
  const primary = primaryId(members)
  const hasCustomWeights = new Set(members.map((member) => member.weight)).size > 1
  return members.map((member) => ({
    ...member,
    priority: mode === "failover" && member.backend_id !== primary ? 1 : 0,
    weight: mode === "weighted"
      ? hasCustomWeights ? Math.max(1, member.weight) : member.backend_id === primary ? 2 : 1
      : 1,
  }))
}

export function addMember(members: PoolMember[], backendId: string, mode: DeploymentMode | null) {
  const nextMode = mode === "balanced" || mode === "weighted" ? mode : "failover"
  const isFirst = members.length === 0
  return [
    ...members,
    { backend_id: backendId, priority: nextMode !== "failover" || isFirst ? 0 : 1, weight: 1 },
  ]
}

/** Adds the backends that are not members yet, in the mode the pool is already in. */
export function appendMembers(members: PoolMember[], backendIds: string[]) {
  const present = new Set(members.map((member) => member.backend_id.toLowerCase()))
  const mode = members.length ? deploymentMode(members) : null
  const added: string[] = []
  let next = members
  for (const backendId of backendIds) {
    if (present.has(backendId.toLowerCase())) continue
    present.add(backendId.toLowerCase())
    next = addMember(next, backendId, mode)
    added.push(backendId)
  }
  return { members: next, added }
}

export function removeMember(members: PoolMember[], backendId: string, mode: DeploymentMode | null) {
  const remaining = members.filter((member) => member.backend_id !== backendId)
  // Removing the primary promotes the next member; re-derive priorities so failover stays valid.
  return mode && mode !== "custom" && members[0]?.backend_id === backendId
    ? membersForMode(remaining, mode)
    : remaining
}

export function setPrimary(members: PoolMember[], backendId: string, mode: DeploymentMode | null) {
  const chosen = members.find((member) => member.backend_id === backendId)
  if (!chosen) return members
  const reordered = [chosen, ...members.filter((member) => member.backend_id !== backendId)]
  return mode && mode !== "custom" ? membersForMode(reordered, mode) : reordered
}

export function changeWeight(members: PoolMember[], backendId: string, weight: number) {
  return members.map((member) => member.backend_id === backendId
    ? { ...member, weight: Math.min(100, Math.max(1, Math.round(weight) || 1)) }
    : member)
}

export function trafficShare(members: PoolMember[], backendId: string) {
  const total = members.reduce((sum, member) => sum + member.weight, 0)
  const member = members.find((candidate) => candidate.backend_id === backendId)
  return member && total > 0 ? Math.round(member.weight / total * 100) : 0
}

export function draftIssues(draft: PoolWrite, mode: DeploymentMode | null) {
  const issues: Array<"members" | "weights"> = []
  if (draft.members.length < MIN_MEMBERS || draft.members.length > MAX_MEMBERS) issues.push("members")
  if (mode === "weighted" && new Set(draft.members.map((member) => member.weight)).size < 2) {
    issues.push("weights")
  }
  return issues
}

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
