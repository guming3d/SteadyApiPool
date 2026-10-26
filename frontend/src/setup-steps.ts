// Groups the setup report into wizard steps. Pure, so node --test can import it.
import type { SetupCheck, SetupKind, SetupReport, SetupStatus } from "./types"

export type StepId = "sign_in" | "gateway" | "access" | "foundry"
export type StepState = "ok" | "attention" | "pending" | "waiting"

export const STEPS: StepId[] = ["sign_in", "gateway", "access", "foundry"]

const STEP_KINDS: Record<StepId, SetupKind[]> = {
  sign_in: ["sign_in", "azure_token"],
  gateway: ["gateway_target", "gateway_health"],
  access: ["gateway_access", "discovery", "app_gateway", "app_discovery"],
  foundry: ["gateway_identity", "foundry_access"],
}

const ATTENTION: ReadonlySet<SetupStatus> = new Set(["action", "blocked"])

export const needsAttention = (check: SetupCheck) => ATTENTION.has(check.status)

export function stepChecks(report: SetupReport | undefined, step: StepId) {
  return (report?.checks ?? []).filter((check) => STEP_KINDS[step].includes(check.kind))
}

/** "waiting" when the step was not checked yet, because an earlier step blocks it. */
export function stepState(report: SetupReport | undefined, step: StepId): StepState {
  const checks = stepChecks(report, step)
  // No Foundry members, or a report from before the gateway step: fine once the app can operate.
  if (!checks.length) return (step === "foundry" || step === "gateway") && report?.can_operate ? "ok" : "waiting"
  if (checks.some(needsAttention)) return "attention"
  if (checks.some((check) => check.status === "pending")) return "pending"
  return "ok"
}

export function attentionCount(report: SetupReport | undefined) {
  return (report?.checks ?? []).filter(needsAttention).length
}

/** A step can be opened unless it is still waiting on an earlier one (e.g. the gateway is
 *  deploying, so permissions can't be checked yet). The current step always can. */
export function reachable(report: SetupReport | undefined, step: StepId, current: StepId) {
  return step === current || stepState(report, step) !== "waiting"
}

/** Next is offered when there is a next step and it can be opened. */
export function canAdvance(report: SetupReport | undefined, current: StepId) {
  const next = STEPS[STEPS.indexOf(current) + 1]
  return next !== undefined && reachable(report, next, current)
}

/** The wizard opens on the first step that needs something, else on the last one. */
export function firstStep(report: SetupReport | undefined): StepId {
  return STEPS.find((step) => stepState(report, step) !== "ok") ?? STEPS[STEPS.length - 1]!
}

/** Checks the user can fix in one go, e.g. "Grant all" for several Foundry accounts. */
export function grantable(checks: SetupCheck[]) {
  return checks.filter((check) => check.fix === "grant_foundry_access" && check.subject)
    .map((check) => check.subject!)
}

/** Last path segment of an ARM ID, e.g. the account name. */
export const resourceName = (armId: string | null) => armId?.replace(/\/+$/, "").split("/").pop() ?? ""

/** Whole minutes since ``startedAt`` (ARM's UTC timestamp, often with 7 fractional digits and
 *  sometimes without a zone); null when it is missing or unreadable. */
export function elapsedMinutes(startedAt: string | null, now: number): number | null {
  if (!startedAt) return null
  const iso = startedAt.replace(/(\.\d{3})\d+/, "$1")
  const started = Date.parse(/(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
  // Azure fills some dates with 0001-01-01 until a resource exists; that is not a start time.
  if (Number.isNaN(started) || started < Date.UTC(2000, 0, 1)) return null
  // A clock a little behind Azure's would otherwise show a negative time.
  return Math.max(0, Math.floor((now - started) / 60_000))
}
