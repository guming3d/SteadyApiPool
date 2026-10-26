// What the start-up screen lists while GET /api/setup runs; see components/startup-progress.tsx.
import type { SetupProgress, SetupStepKind } from "./types"

export type ProgressRow = {
  key: SetupStepKind | "connecting"
  running: boolean
  subject?: string
  count?: { done: number; total: number }
}

/** After this long the screen adds that slow sign-ins and networks take longer. */
export const SLOW_SECONDS = 40

/** One row per step reached so far; before the report starts, the request is still on its way. */
export function progressRows(progress: SetupProgress | undefined): ProgressRow[] {
  if (!progress?.steps.length) return [{ key: "connecting", running: true }]
  return progress.steps.map((step) => ({
    key: step.kind,
    running: step.state === "running",
    ...(step.subject ? { subject: step.subject } : {}),
    ...(step.total ? { count: { done: Math.min(step.done ?? 0, step.total), total: step.total } } : {}),
  }))
}
