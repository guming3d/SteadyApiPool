// What publishing a pool draft changes, in the terms the editor shows: roles and traffic share.
import { deploymentMode, type DeploymentMode } from "./pool-modes.ts"
import { route } from "./routing-sim.ts"
import type { PoolMember, PoolWrite } from "./types"

export type MemberRole = "primary" | "standby" | "member"

export type PoolChange =
  | { kind: "created"; mode: DeploymentMode; count: number }
  | { kind: "mode"; from: DeploymentMode; to: DeploymentMode }
  | { kind: "added"; id: string; role: MemberRole; share: number }
  | { kind: "removed"; id: string }
  | { kind: "role"; id: string; from: MemberRole; to: MemberRole }
  | { kind: "share"; id: string; from: number; to: number }
  | { kind: "affinity"; to: boolean }
  | { kind: "title"; to: string | null }
  // Members whose circuit breaker publishing rewrites to the pool's rule (see pool-breaker.ts).
  | { kind: "breaker"; count: number }

/** Same roles as the member list: the first member is primary, the rest standby in failover. */
export function memberRole(members: PoolMember[], backendId: string): MemberRole {
  if (members[0]?.backend_id === backendId) return "primary"
  return deploymentMode(members) === "failover" ? "standby" : "member"
}

const key = (backendId: string) => backendId.toLowerCase()

export function poolChanges(before: PoolWrite | null, after: PoolWrite): PoolChange[] {
  const afterShares = route(after.members).shares
  const share = (shares: Map<string, number>, backendId: string) => Math.round(shares.get(backendId) ?? 0)
  if (!before) {
    return [
      { kind: "created", mode: deploymentMode(after.members), count: after.members.length },
      ...after.members.map((member): PoolChange => ({
        kind: "added",
        id: member.backend_id,
        role: memberRole(after.members, member.backend_id),
        share: share(afterShares, member.backend_id),
      })),
      ...after.session_affinity ? [{ kind: "affinity", to: true } as const] : [],
    ]
  }

  const changes: PoolChange[] = []
  const beforeShares = route(before.members).shares
  const beforeMode = deploymentMode(before.members)
  const afterMode = deploymentMode(after.members)
  if (before.members.length && after.members.length && beforeMode !== afterMode) {
    changes.push({ kind: "mode", from: beforeMode, to: afterMode })
  }
  const previous = new Map(before.members.map((member) => [key(member.backend_id), member]))
  const next = new Set(after.members.map((member) => key(member.backend_id)))
  for (const member of after.members) {
    const old = previous.get(key(member.backend_id))
    const role = memberRole(after.members, member.backend_id)
    const to = share(afterShares, member.backend_id)
    if (!old) {
      changes.push({ kind: "added", id: member.backend_id, role, share: to })
      continue
    }
    const oldRole = memberRole(before.members, old.backend_id)
    const from = share(beforeShares, old.backend_id)
    if (oldRole !== role) changes.push({ kind: "role", id: member.backend_id, from: oldRole, to: role })
    if (from !== to) changes.push({ kind: "share", id: member.backend_id, from, to })
  }
  for (const member of before.members) {
    if (!next.has(key(member.backend_id))) changes.push({ kind: "removed", id: member.backend_id })
  }
  if (before.session_affinity !== after.session_affinity) changes.push({ kind: "affinity", to: after.session_affinity })
  if ((before.title ?? null) !== (after.title ?? null)) changes.push({ kind: "title", to: after.title ?? null })
  return changes
}
