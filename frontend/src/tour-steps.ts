// The first-run product tour: which part of the page each step points at, and where its card
// goes. Pure, so node --test can import it; the overlay is components/product-tour.tsx.

export type Placement = "top" | "bottom" | "left" | "right" | "center"

export type TourStep = {
  id: string
  /** data-tour value of the element to highlight; none for a centred card. */
  target?: string
  /** Clicked first (if present) so the step's view is showing, e.g. a tab. */
  ensure?: string
  /** The user clicks the highlighted element to go on; Next clicks it for them. */
  action?: "click"
  /** Skipped unless this data-tour element is on the page, e.g. a selected pool. */
  requires?: string
  placement?: Placement
}

// Bumped when the tour changes enough to show it again to people who finished it.
export const TOUR_VERSION = "1"
export const TOUR_STORAGE_KEY = "ai-gateway-pool:tour"

/** Deployments → pools → one pool's tabs: the order a first pool is built in. */
export const TOUR_STEPS: TourStep[] = [
  { id: "welcome", placement: "center" },
  { id: "gateway", target: "gateway", placement: "bottom" },
  { id: "setup", target: "setup", placement: "bottom" },
  { id: "tabDeployments", target: "tab-deployments", action: "click", placement: "bottom" },
  { id: "deployments", target: "deployments-list", ensure: "tab-deployments", placement: "top" },
  { id: "tabPools", target: "tab-pools", action: "click", placement: "bottom" },
  { id: "addPool", target: "add-pool", ensure: "tab-pools", placement: "left" },
  { id: "poolList", target: "pool-list", ensure: "tab-pools", placement: "right" },
  { id: "poolSummary", target: "pool-summary", ensure: "tab-pools", requires: "pool-row", placement: "bottom" },
  { id: "poolMode", target: "pool-mode", ensure: "pool-tab-members", requires: "pool-row", placement: "bottom" },
  { id: "poolRouting", target: "pool-tab-routing", action: "click", requires: "pool-row", placement: "bottom" },
  { id: "poolConnect", target: "pool-tab-connect", action: "click", requires: "pool-row", placement: "bottom" },
  { id: "poolMonitor", target: "pool-tab-monitor", action: "click", requires: "pool-row", placement: "bottom" },
  { id: "glossary", target: "glossary", requires: "pool-row", placement: "left" },
  { id: "done", placement: "center" },
]

/**
 * Whether a step can show, given which data-tour elements are on the page: its ``requires``
 * element must be there (once its view is open), and so must its target. A step without a
 * target is a centred card and always shows.
 */
export function stepAvailable(step: TourStep, present: (target: string) => boolean) {
  if (step.requires && !present(step.requires)) return false
  return !step.target || present(step.target)
}

/** Fraction of the tour done, for the progress bar. */
export const progress = (index: number, total: number) => total > 1 ? index / (total - 1) : 1

export type Rect = { top: number; left: number; width: number; height: number }
export type Size = { width: number; height: number }

const OPPOSITE: Record<Exclude<Placement, "center">, Exclude<Placement, "center">> = {
  top: "bottom",
  bottom: "top",
  left: "right",
  right: "left",
}

/**
 * Where the card goes: on the preferred side of the target if it fits, else the opposite side,
 * else whichever side has the most room; always kept inside the viewport by ``margin``.
 */
export function placeCard(target: Rect | null, card: Size, viewport: Size, preferred: Placement = "bottom", gap = 12, margin = 12) {
  const clampLeft = (left: number) => Math.min(Math.max(left, margin), viewport.width - card.width - margin)
  const clampTop = (top: number) => Math.min(Math.max(top, margin), viewport.height - card.height - margin)
  if (!target || preferred === "center") {
    return { top: clampTop((viewport.height - card.height) / 2), left: clampLeft((viewport.width - card.width) / 2), placement: "center" as Placement }
  }
  const room = {
    top: target.top - gap - margin,
    bottom: viewport.height - (target.top + target.height) - gap - margin,
    left: target.left - gap - margin,
    right: viewport.width - (target.left + target.width) - gap - margin,
  }
  const fits = (side: Exclude<Placement, "center">) =>
    room[side] >= (side === "top" || side === "bottom" ? card.height : card.width)
  const sides = (Object.keys(room) as Array<keyof typeof room>).sort((a, b) => room[b] - room[a])
  const side = [preferred, OPPOSITE[preferred]].find(fits) ?? sides[0]!
  const centreX = target.left + target.width / 2 - card.width / 2
  const centreY = target.top + target.height / 2 - card.height / 2
  switch (side) {
    case "top": return { top: clampTop(target.top - gap - card.height), left: clampLeft(centreX), placement: side }
    case "bottom": return { top: clampTop(target.top + target.height + gap), left: clampLeft(centreX), placement: side }
    case "left": return { top: clampTop(centreY), left: clampLeft(target.left - gap - card.width), placement: side }
    case "right": return { top: clampTop(centreY), left: clampLeft(target.left + target.width + gap), placement: side }
  }
}

/** The spotlight hole: the target plus some padding, inside the viewport. */
export function spotlight(target: Rect, viewport: Size, padding = 6): Rect {
  const top = Math.max(0, target.top - padding)
  const left = Math.max(0, target.left - padding)
  return {
    top,
    left,
    width: Math.min(viewport.width, target.left + target.width + padding) - left,
    height: Math.min(viewport.height, target.top + target.height + padding) - top,
  }
}

/** Shown once, after setup lets the app work and while nothing else is in the way. */
export function shouldAutoStart(stored: string | null, canOperate: boolean, setupOpen: boolean) {
  return canOperate && !setupOpen && stored !== TOUR_VERSION
}
