import assert from "node:assert/strict"
import test from "node:test"

import {
  actualShares, availabilityTone, formatCompact, formatMs, formatPercent, niceScale, perMinute,
  roundedTop, shareTone, throttleTone,
} from "../src/pool-metrics.ts"

test("numbers compact from 10,000 up", () => {
  assert.equal(formatCompact(1284), "1,284")
  assert.equal(formatCompact(12_900), "12.9K")
  assert.equal(formatCompact(4_200_000), "4.2M")
  assert.equal(formatCompact(250_000), "250K")
  assert.equal(formatCompact(0), "0")
})

test("durations switch to seconds at one second", () => {
  assert.equal(formatMs(820.4), "820 ms")
  assert.equal(formatMs(1430), "1.43 s")
  assert.equal(formatMs(13153), "13.2 s")
  assert.equal(formatMs(null), "—")
})

test("percentages never round up to a false 100%", () => {
  assert.equal(formatPercent(0.99952, 2), "99.95%")
  assert.equal(formatPercent(0.99999, 2), "99.99%")
  assert.equal(formatPercent(1, 2), "100%")
  assert.equal(formatPercent(0.004), "0.4%")
  assert.equal(formatPercent(null), "—")
})

test("axis maxima are clean numbers", () => {
  assert.deepEqual(niceScale(37), { max: 50, ticks: [0, 25, 50] })
  assert.deepEqual(niceScale(1800), { max: 2000, ticks: [0, 1000, 2000] })
  assert.deepEqual(niceScale(0), { max: 1, ticks: [0, 1] })
})

test("health thresholds", () => {
  assert.equal(availabilityTone(0.9995), "ok")
  assert.equal(availabilityTone(0.995), "warn")
  assert.equal(availabilityTone(0.9), "bad")
  assert.equal(availabilityTone(null), "ok")
  assert.equal(throttleTone(0, 1000), "ok")
  assert.equal(throttleTone(10, 1000), "warn")
  assert.equal(throttleTone(395, 735), "bad")
  assert.equal(throttleTone(5, 0), "ok")
})

test("traffic shares compare with what priority and weight intend", () => {
  const member = (requests) => ({ requests })
  assert.deepEqual(actualShares([member(75), member(25), member(null)]), [0.75, 0.25, null])
  assert.deepEqual(actualShares([member(0), member(0)]), [null, null])
  assert.equal(shareTone(0.55, 0.5), "ok")
  assert.equal(shareTone(0.75, 0.5), "warn")
  assert.equal(shareTone(1, 0.5), "bad")
  assert.equal(shareTone(null, 0.5), "ok")
})

test("rates and shapes", () => {
  assert.equal(perMinute(600, "2026-10-24T06:00:00Z", "2026-10-24T07:00:00Z"), 10)
  assert.equal(roundedTop(0, 10, 20, 30), "M0,40V14Q0,10 4,10H16Q20,10 20,14V40Z")
  // Too short for the full radius: the corner shrinks instead of overshooting.
  assert.equal(roundedTop(0, 10, 20, 2), "M0,12V12Q0,10 2,10H18Q20,10 20,12V12Z")
})
