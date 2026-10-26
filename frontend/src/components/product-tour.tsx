// First-run product tour: spotlights one part of the page at a time and explains it in plain
// words, for people new to API Management. Steps and card placement live in tour-steps.ts.
import { ArrowLeft, ArrowRight, Check, MousePointerClick, X } from "lucide-react"
import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"

import { useI18n, type MessageKey } from "../i18n"
import { placeCard, progress, spotlight, stepAvailable, TOUR_STEPS, type Rect } from "../tour-steps"
import { Button } from "./ui"

const find = (target: string) => document.querySelector<HTMLElement>(`[data-tour="${target}"]`)
// Elements in hidden tab panels have no boxes.
const shown = (element: HTMLElement | null): element is HTMLElement => Boolean(element?.getClientRects().length)
const present = (target: string) => shown(find(target))
const viewport = () => ({ width: window.innerWidth, height: window.innerHeight })
// How long a step waits for its view (a tab switch, data loading) before it's skipped.
const REQUIRES_WAIT_MS = 400
const TARGET_WAIT_MS = 2500

function measure(element: HTMLElement): Rect {
  const box = element.getBoundingClientRect()
  return { top: box.top, left: box.left, width: box.width, height: box.height }
}

/** Closed when finished or skipped; either way the caller marks it seen. */
export function ProductTour({ onClose }: { onClose: () => void }) {
  const { t } = useI18n()
  const [index, setIndex] = useState(0)
  const [target, setTarget] = useState<Rect | null>(null)
  const [ready, setReady] = useState(false)
  const [cardSize, setCardSize] = useState({ width: 360, height: 220 })
  const direction = useRef<1 | -1>(1)
  const cardRef = useRef<HTMLDivElement>(null)
  const nextRef = useRef<HTMLButtonElement>(null)
  const step = TOUR_STEPS[index]!
  const reduced = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  const last = index === TOUR_STEPS.length - 1

  const move = (to: 1 | -1) => {
    direction.current = to
    const next = index + to
    if (next < 0) return
    if (next >= TOUR_STEPS.length) return onClose()
    setIndex(next)
  }

  // Open the step's view, wait for its element, and skip it if it never appears (no pool yet,
  // read-only users without an Add pool button).
  useEffect(() => {
    setReady(false)
    setTarget(null)
    if (!step.target) {
      setReady(true)
      return
    }
    if (step.ensure) {
      const opener = find(step.ensure)
      if (opener && opener.getAttribute("aria-selected") !== "true") opener.click()
    }
    const started = performance.now()
    let frame = 0
    const poll = () => {
      const elapsed = performance.now() - started
      const element = find(step.target!)
      if (shown(element) && stepAvailable(step, present)) {
        element.scrollIntoView({ block: "center", inline: "nearest", behavior: reduced ? "auto" : "smooth" })
        setTarget(measure(element))
        setReady(true)
        return
      }
      if ((step.requires && elapsed > REQUIRES_WAIT_MS && !present(step.requires)) || elapsed > TARGET_WAIT_MS) {
        const next = index + direction.current
        if (next < 0 || next >= TOUR_STEPS.length) return onClose()
        setIndex(next)
        return
      }
      frame = requestAnimationFrame(poll)
    }
    poll()
    return () => cancelAnimationFrame(frame)
  }, [index])

  // Follow the element while the page scrolls, resizes or reflows (smooth scrolling, data).
  useEffect(() => {
    if (!ready || !step.target) return
    const update = () => {
      const element = find(step.target!)
      if (shown(element)) setTarget(measure(element))
    }
    const timer = window.setInterval(update, 200)
    window.addEventListener("resize", update)
    window.addEventListener("scroll", update, true)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener("resize", update)
      window.removeEventListener("scroll", update, true)
    }
  }, [ready, index])

  // Steps that ask for a click go on when the user clicks the highlighted element.
  useEffect(() => {
    if (!ready || step.action !== "click" || !step.target) return
    const element = find(step.target)
    if (!element) return
    const clicked = () => move(1)
    element.addEventListener("click", clicked)
    return () => element.removeEventListener("click", clicked)
  }, [ready, index])

  useLayoutEffect(() => {
    if (!cardRef.current) return
    const box = cardRef.current.getBoundingClientRect()
    if (Math.abs(box.width - cardSize.width) > 1 || Math.abs(box.height - cardSize.height) > 1) {
      setCardSize({ width: box.width, height: box.height })
    }
  })

  useEffect(() => {
    if (ready) nextRef.current?.focus({ preventScroll: true })
  }, [ready, index])

  useEffect(() => {
    const keys = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose()
      else if (event.key === "ArrowRight") next()
      else if (event.key === "ArrowLeft") move(-1)
    }
    window.addEventListener("keydown", keys)
    return () => window.removeEventListener("keydown", keys)
  })

  const next = () => {
    // "Click Deployments": Next does the click (whose listener moves on), so the following
    // steps find their view.
    const element = ready && step.action === "click" && step.target ? find(step.target) : null
    if (element) element.click()
    else move(1)
  }

  const view = viewport()
  const hole = target ? spotlight(target, view) : null
  const position = placeCard(hole, cardSize, view, step.placement)
  const key = (suffix: "Title" | "Body") => `tour${step.id[0]!.toUpperCase()}${step.id.slice(1)}${suffix}` as MessageKey
  const blockers = hole
    ? [
      { top: 0, left: 0, width: view.width, height: hole.top },
      { top: hole.top + hole.height, left: 0, width: view.width, height: Math.max(0, view.height - hole.top - hole.height) },
      { top: hole.top, left: 0, width: hole.left, height: hole.height },
      { top: hole.top, left: hole.left + hole.width, width: Math.max(0, view.width - hole.left - hole.width), height: hole.height },
    ]
    : [{ top: 0, left: 0, width: view.width, height: view.height }]

  return createPortal(<div className="tour" data-motion={reduced ? "reduced" : "full"}>
    {/* Clicks outside the highlighted element do nothing while the tour is open. */}
    {blockers.map((box, position) => <div key={position} className="tour-blocker" style={box} />)}
    {hole
      ? <div className="tour-spotlight" style={hole} data-action={step.action === "click"} />
      : <div className="tour-dim" />}
    {/* Explained, not clicked: the element itself stays inert unless the step asks for a click. */}
    {hole && step.action !== "click" && <div className="tour-blocker" style={hole} />}
    <div ref={cardRef} className="tour-card" role="dialog" aria-modal="false" aria-labelledby="tour-title" aria-describedby="tour-body"
      style={{ top: position.top, left: position.left, visibility: ready ? "visible" : "hidden" }} data-placement={position.placement}>
      <div className="tour-progress" aria-hidden="true"><span style={{ width: `${progress(index, TOUR_STEPS.length) * 100}%` }} /></div>
      <header>
        <b id="tour-title">{t[key("Title")]}</b>
        <button type="button" className="icon-button" aria-label={t.tourSkip} title={t.tourSkip} onClick={onClose}><X size={14} /></button>
      </header>
      <p id="tour-body">{t[key("Body")]}</p>
      {step.action === "click" && <p className="tour-hint"><MousePointerClick size={13} />{t.tourClickHint}</p>}
      <footer>
        {!last && <Button variant="ghost" onClick={onClose}>{t.tourSkip}</Button>}
        <span className="tour-spacer" />
        {index > 0 && <Button onClick={() => move(-1)}><ArrowLeft size={13} />{t.tourBack}</Button>}
        <Button ref={nextRef} variant="primary" onClick={next}>
          {last ? <><Check size={13} />{t.tourDone}</> : <>{t.tourNext}<ArrowRight size={13} /></>}
        </Button>
      </footer>
    </div>
  </div>, document.body)
}
