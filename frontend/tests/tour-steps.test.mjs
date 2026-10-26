import assert from "node:assert/strict"
import test from "node:test"

import { placeCard, progress, shouldAutoStart, spotlight, stepAvailable, TOUR_STEPS, TOUR_VERSION } from "../src/tour-steps.ts"

const viewport = { width: 1200, height: 800 }
const card = { width: 360, height: 200 }

test("the tour follows the order a first pool is built in", () => {
  const ids = TOUR_STEPS.map((step) => step.id)
  assert.equal(ids[0], "welcome")
  assert.equal(ids.at(-1), "done")
  assert.ok(ids.indexOf("tabDeployments") < ids.indexOf("tabPools"))
  // Members live inside their pool; there is no top-level tab for them.
  assert.ok(!ids.includes("tabBackends"))
  assert.equal(new Set(ids).size, ids.length)
  // Steps inside a pool only show when one is selected.
  for (const id of ["poolSummary", "poolMode", "poolRouting", "poolConnect", "poolMonitor", "glossary"]) {
    assert.equal(TOUR_STEPS.find((step) => step.id === id).requires, "pool-row", id)
  }
})

test("a step shows only when its target and requirement are on the page", () => {
  const step = { id: "x", target: "pool-summary", requires: "pool-row" }
  assert.equal(stepAvailable(step, () => true), true)
  assert.equal(stepAvailable(step, (target) => target !== "pool-row"), false)
  assert.equal(stepAvailable(step, (target) => target !== "pool-summary"), false)
  assert.equal(stepAvailable({ id: "welcome" }, () => false), true)
})

test("the card sits on the preferred side when it fits, else flips", () => {
  const tab = { top: 10, left: 500, width: 120, height: 32 }
  const below = placeCard(tab, card, viewport, "top")
  assert.equal(below.placement, "bottom")  // No room above a top-bar tab.
  assert.equal(below.top, 10 + 32 + 12)
  const button = { top: 300, left: 1000, width: 120, height: 32 }
  const left = placeCard(button, card, viewport, "left")
  assert.equal(left.placement, "left")
  assert.equal(left.left, 1000 - 12 - 360)
})

test("the card always stays inside the viewport", () => {
  for (const target of [
    { top: 0, left: 0, width: 50, height: 20 },
    { top: 780, left: 1180, width: 20, height: 20 },
    { top: 0, left: 0, width: 1200, height: 800 },
  ]) {
    for (const preferred of ["top", "bottom", "left", "right"]) {
      const place = placeCard(target, card, viewport, preferred)
      assert.ok(place.left >= 12 && place.left + card.width <= viewport.width - 12, `${preferred} ${JSON.stringify(place)}`)
      assert.ok(place.top >= 12 && place.top + card.height <= viewport.height - 12, `${preferred} ${JSON.stringify(place)}`)
    }
  }
  const centred = placeCard(null, card, viewport)
  assert.deepEqual(centred, { top: 300, left: 420, placement: "center" })
})

test("the spotlight pads the target and stays on screen", () => {
  assert.deepEqual(spotlight({ top: 100, left: 100, width: 50, height: 20 }, viewport), { top: 94, left: 94, width: 62, height: 32 })
  assert.deepEqual(spotlight({ top: 2, left: 2, width: 50, height: 20 }, viewport), { top: 0, left: 0, width: 58, height: 28 })
})

test("auto-start once, after setup, and again only for a new tour version", () => {
  assert.equal(shouldAutoStart(null, true, false), true)
  assert.equal(shouldAutoStart(null, false, false), false)
  assert.equal(shouldAutoStart(null, true, true), false)
  assert.equal(shouldAutoStart(TOUR_VERSION, true, false), false)
  assert.equal(shouldAutoStart("0", true, false), true)
  assert.equal(progress(0, 17), 0)
  assert.equal(progress(16, 17), 1)
})
