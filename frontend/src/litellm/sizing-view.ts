// The capacity sizer's inputs and how its plan reads. Pure, so node --test can import it;
// capacity-sizer.tsx shows it. The arithmetic itself is the backend's (litellm/sizing.py): the
// browser only words the numbers the plan carries.
import type { GatewayPlan, GatewaySizing, ModelSpeed, PlanNote, ResizeChange } from "./types"

export const DEFAULT_SIZING: GatewaySizing = {
  peak_rpm: 600,
  input_tokens: 2000,
  output_tokens: 500,
  timeout_seconds: 120,
  streaming: true,
  model_speed: "standard",
  response_seconds: null,
  headroom_percent: 25,
  zone_redundant: true,
  kubernetes_team: false,
  self_hosted_models: false,
  private_network: false,
}

export const MODEL_SPEEDS: ModelSpeed[] = ["fast", "standard", "reasoning"]

type NumberField = "peak_rpm" | "input_tokens" | "output_tokens" | "timeout_seconds" | "headroom_percent"

/** Mirrors GatewaySizing's bounds in pool_manager/litellm/models.py. */
export const LIMITS: Record<NumberField, { min: number; max: number }> = {
  peak_rpm: { min: 1, max: 1_000_000 },
  input_tokens: { min: 1, max: 2_000_000 },
  output_tokens: { min: 1, max: 200_000 },
  timeout_seconds: { min: 10, max: 3600 },
  headroom_percent: { min: 0, max: 200 },
}
export const RESPONSE_SECONDS_MAX = 3600

/** The fields outside their bounds; the sizer asks for a plan only when there are none. */
export function invalidFields(sizing: GatewaySizing): string[] {
  const invalid: string[] = (Object.keys(LIMITS) as NumberField[]).filter((field) => {
    const value = sizing[field]
    return !Number.isInteger(value) || value < LIMITS[field].min || value > LIMITS[field].max
  })
  const response = sizing.response_seconds
  if (response !== null && !(response > 0 && response <= RESPONSE_SECONDS_MAX)) invalid.push("response_seconds")
  return invalid
}

/** A number field's text as the scenario's value: whole numbers, nothing when empty. */
export function parseCount(text: string): number {
  const value = Number(text.replace(/[,\s_]/g, ""))
  return Number.isFinite(value) ? Math.round(value) : Number.NaN
}

/** The UI's message key for a plan note (litellm/messages.ts). */
export const noteKey = (note: PlanNote) => `planNote_${note.code}` as const

/** A note's values with numbers formatted for the locale (tokens a minute as quota reads). */
export function noteValues(note: PlanNote, locale: string): Record<string, string> {
  const text = (key: string, value: number | string) =>
    typeof value !== "number" ? value : key === "tpm" ? formatTpm(value, locale) : formatNumber(value, locale)
  return Object.fromEntries(Object.entries(note.values).map(([key, value]) => [key, text(key, value)]))
}

export function formatNumber(value: number, locale: string, digits = 0): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: digits }).format(value)
}

/** Millions of tokens a minute, as people quote Foundry quota. */
export function formatTpm(tpm: number, locale: string): string {
  return tpm >= 1_000_000 ? `${formatNumber(tpm / 1_000_000, locale, 1)}M` : `${formatNumber(tpm / 1000, locale, 0)}K`
}

export type StepKey =
  | "planStepRps" | "planStepTpm" | "planStepCall" | "planStepInFlight" | "planStepCpu"
  | "planStepHolds" | "planStepReplicas" | "planStepReplicasSingle" | "planStepScaleAca" | "planStepScaleAks" | "planStepNodes"
  | "planStepOutbound" | "planStepDatabase" | "planStepRedis" | "planStepNoRedis" | "planStepBandwidth"
  | "planStepTimeoutAca" | "planStepTimeoutAks"

export type Step = { key: StepKey; values: Record<string, string> }

/** "How we got this": each step of the arithmetic with its numbers, in order. */
export function planSteps(plan: GatewayPlan, sizing: GatewaySizing, locale: string): Step[] {
  const n = (value: number, digits = 0) => formatNumber(value, locale, digits)
  const range = (low: number, high: number) => (low === high ? n(low) : `${n(low)}–${n(high)}`)
  const steps: Step[] = [
    { key: "planStepRps", values: { rps: n(plan.rps, 1), rpm: n(sizing.peak_rpm) } },
    { key: "planStepTpm", values: { tpm: formatTpm(plan.tpm, locale), tokens: n(sizing.input_tokens + sizing.output_tokens) } },
    { key: "planStepCall", values: { seconds: n(plan.call_seconds, 1) } },
    { key: "planStepInFlight", values: { calls: n(plan.in_flight), slow: n(plan.in_flight_slow), timeout: n(sizing.timeout_seconds) } },
    { key: "planStepCpu", values: { cpu: n(plan.cpu_ms, 1), rps: n(plan.replica_rps, 1) } },
    { key: "planStepHolds", values: { calls: n(plan.replica_in_flight), memory: n(plan.replica_memory_gib) } },
    {
      key: sizing.zone_redundant ? "planStepReplicas" : "planStepReplicasSingle",
      values: { peak: n(plan.peak_replicas), range: range(plan.min_replicas, plan.max_replicas), headroom: n(sizing.headroom_percent) },
    },
  ]
  if (plan.aks) {
    steps.push(
      { key: "planStepScaleAks", values: {} },
      {
        key: "planStepNodes",
        values: { nodes: range(plan.aks.node_min, plan.aks.node_max), size: plan.aks.node_size.replace(/^Standard_/, ""), pods: n(plan.aks.pods_per_node), tier: plan.aks.tier },
      },
      { key: "planStepOutbound", values: { ips: n(plan.aks.outbound_ips), ports: n(plan.aks.outbound_ports), needed: n(plan.aks.ports_needed) } },
    )
  } else {
    steps.push({ key: "planStepScaleAca", values: { rps: n(plan.scale_rps) } })
  }
  steps.push(
    {
      key: "planStepDatabase",
      values: { sku: plan.postgres_sku.replace(/^Standard_/, ""), connections: n(plan.postgres_connections), limit: n(plan.postgres_limit), pool: n(plan.db_pool) },
    },
    plan.redis_sku ? { key: "planStepRedis", values: { sku: plan.redis_sku.replace("_", " ") } } : { key: "planStepNoRedis", values: {} },
    { key: "planStepBandwidth", values: { mbps: n(plan.bandwidth_mbps, 1) } },
    plan.aks
      ? { key: "planStepTimeoutAks", values: { timeout: n(plan.request_timeout), route: n(plan.route_timeout) } }
      : { key: "planStepTimeoutAca", values: { timeout: n(plan.request_timeout) } },
  )
  return steps
}

/** The plan in one line's values: replicas (or nodes), database, Redis. */
export function planSummary(plan: GatewayPlan, locale: string): Record<string, string> {
  const n = (value: number) => formatNumber(value, locale)
  const range = (low: number, high: number) => (low === high ? n(low) : `${n(low)}–${n(high)}`)
  return {
    replicas: range(plan.min_replicas, plan.max_replicas),
    nodes: plan.aks ? `${range(plan.aks.node_min, plan.aks.node_max)} × ${plan.aks.node_size.replace(/^Standard_/, "")}` : "",
    database: plan.postgres_sku.replace(/^Standard_/, ""),
    redis: plan.redis_sku ? plan.redis_sku.replace("_", " ") : "",
  }
}

export type ImpactKey = "resizeImpact_revision" | "resizeImpact_nodes" | "resizeImpact_pods" | "resizeImpact_database"

/** What a resize does to running traffic, for the warning before it is applied. */
export function resizeImpacts(changes: ResizeChange[], database: boolean): ImpactKey[] {
  const applied = new Set(changes.filter((change) => change.action === "apply").map((change) => change.item))
  const impacts: ImpactKey[] = []
  if (applied.has("replicas") || applied.has("scale_rule") || applied.has("replica_size")) impacts.push("resizeImpact_revision")
  if (applied.has("nodes")) impacts.push("resizeImpact_nodes")
  if (applied.has("pods")) impacts.push("resizeImpact_pods")
  if (database && changes.some((change) => change.item === "database")) impacts.push("resizeImpact_database")
  return impacts
}

/** Whether there is anything the app would change: applied items, or the database when asked. */
export function resizeChangesSomething(changes: ResizeChange[], database: boolean): boolean {
  return changes.some((change) => change.action === "apply" || (database && change.action === "optional"))
}
