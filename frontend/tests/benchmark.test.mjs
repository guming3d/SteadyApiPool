import assert from "node:assert/strict"
import test from "node:test"

import {
  chartPoints, cliCommand, DEFAULT_SPEC, estimate, invalidFields, presetOf, presetSpec, runJson, timelineCsv, verdict,
} from "../src/benchmark.ts"

const summary = (overrides = {}) => ({
  elapsed_seconds: 30, requests: 100, successes: 100, throttled: 0, errors: {}, requests_per_second: 3.33,
  success_rate: 1, latency_p50_ms: 900, latency_p90_ms: 1500, latency_p95_ms: 2100, latency_p99_ms: 2600,
  latency_max_ms: 3000, ttft_p50_ms: 300, ttft_p95_ms: 600, prompt_tokens: 5000, completion_tokens: 2000,
  output_tokens_per_second: 66.7, failovers: 0, members: [], timeline: [], ...overrides,
})

test("presets set the load and keep the rest", () => {
  const typical = presetSpec("typical", { ...DEFAULT_SPEC, max_tokens: 256 })
  assert.equal(typical.users, 10)
  assert.equal(typical.duration_seconds, 120)
  assert.equal(typical.ramp_up_seconds, 10)
  assert.equal(typical.max_tokens, 256)
  assert.equal(presetOf(typical), "typical")
  assert.equal(presetOf(DEFAULT_SPEC), "smoke")
  assert.equal(presetOf({ ...DEFAULT_SPEC, users: 3 }), null)
})

test("limits", () => {
  assert.deepEqual(invalidFields(DEFAULT_SPEC), [])
  assert.deepEqual(invalidFields({ ...DEFAULT_SPEC, users: 51, duration_seconds: 0, rate: 0, max_tokens: 1.5 }), ["users", "duration_seconds", "rate", "max_tokens"])
})

test("the estimate: users times time over latency, capped by the rate and max requests", () => {
  // 2 users for 30 s at about 1.94 s a request.
  assert.equal(estimate(DEFAULT_SPEC).requests, 31)
  assert.equal(estimate({ ...DEFAULT_SPEC, rate: 0.5 }).requests, 15)
  assert.equal(estimate({ ...DEFAULT_SPEC, max_requests: 10 }).requests, 10)
  // Users joining over the run send less.
  assert.ok(estimate({ ...DEFAULT_SPEC, users: 10, ramp_up_seconds: 30 }).requests < estimate({ ...DEFAULT_SPEC, users: 10 }).requests)
  const long = estimate({ ...DEFAULT_SPEC, prompt_size: "long", max_requests: 10 })
  assert.deepEqual([long.inputTokens, long.outputTokens], [40000, 1280])
})

test("the verdict", () => {
  assert.deepEqual(verdict(summary()), {
    tone: "ok",
    parts: [{ key: "benchVerdictRate", values: { rate: "3.3", p95: "2.1 s" } }, { key: "benchVerdictOk", values: {} }],
  })
  const throttled = verdict(summary({ throttled: 3, successes: 97, success_rate: 0.97, failovers: 31 }))
  assert.equal(throttled.tone, "warn")
  assert.deepEqual(throttled.parts.slice(1), [
    { key: "benchVerdictThrottled", values: { count: 3, percent: "3%" } },
    { key: "benchVerdictFailovers", values: { count: 31 } },
  ])
  assert.deepEqual(verdict(summary({ success_rate: 0.5, errors: { 401: 50 } })).parts[1], { key: "benchVerdictErrors", values: { percent: "50%" } })
  assert.equal(verdict(summary({ success_rate: 0.5 })).tone, "bad")
  assert.deepEqual(verdict(summary({ requests: 0, success_rate: null })).parts, [{ key: "benchVerdictNone", values: {} }])
})

test("chart buckets average per second", () => {
  const timeline = Array.from({ length: 180 }, (_, second) => ({
    second, requests: 4, errors: second === 0 ? 1 : 0, throttled: 0, latency_p50_ms: second < 2 ? 1000 : 2000, users: 2,
  }))
  const points = chartPoints(timeline, 90)
  assert.equal(points.length, 90)
  assert.deepEqual(points[0], { second: 0, seconds: 2, success: 3.5, throttled: 0, errors: 0.5, latencyMs: 1000 })
  assert.equal(points[1].latencyMs, 2000)
  assert.equal(chartPoints(timeline.slice(0, 10)).length, 10)
})

test("exports hold no key", () => {
  const run = {
    id: "r1", pool_id: "gpt6", state: "done", spec: DEFAULT_SPEC, model: "gpt6", started_at: "2026-10-27T10:00:00+00:00", error: null,
    summary: summary({ timeline: [{ second: 0, requests: 3, errors: 0, throttled: 1, latency_p50_ms: null, users: 2 }] }),
  }
  assert.equal(timelineCsv(run), "second,users,requests,errors,throttled,latency_p50_ms\n0,2,3,0,1,\n")
  assert.equal(JSON.parse(runJson(run)).id, "r1")
})

test("the terminal command", () => {
  const url = "https://gw.example.com/v1"
  assert.equal(
    cliCommand({ url, model: "gpt6", spec: DEFAULT_SPEC, windows: false }),
    "export LITELLM_KEY='<your-virtual-key>'\nbackend/.venv/bin/python scripts/benchmark.py --url https://gw.example.com/v1 --model gpt6 --users 2 --duration 30",
  )
  const spec = { ...presetSpec("typical"), rate: 5, prompt_size: "long", max_tokens: 64, stream: false }
  assert.equal(
    cliCommand({ url, model: "gpt-4o", spec, windows: true }).split("\n")[1],
    "backend\\.venv\\Scripts\\python scripts\\benchmark.py --url https://gw.example.com/v1 --model gpt-4o --users 10 --duration 120 --ramp-up 10 --rate 5 --prompt long --max-tokens 64 --no-stream",
  )
})

test("the terminal command for an APIM API", () => {
  const url = "https://apim.azure-api.net/gpt-4o"
  const apim = { apiVersion: "2025-04-01-preview", keyHeader: "Ocp-Apim-Subscription-Key" }
  assert.equal(
    cliCommand({ url, model: "gpt-4o", spec: DEFAULT_SPEC, windows: false, apim }),
    "export APIM_KEY='<your-subscription-key>'\nbackend/.venv/bin/python scripts/benchmark.py --gateway apim --url https://apim.azure-api.net/gpt-4o --model gpt-4o --users 2 --duration 30",
  )
  // Only what differs from the script's defaults.
  const other = { apiVersion: "2024-10-21", keyHeader: "api-key" }
  assert.equal(
    cliCommand({ url, model: "gpt-4o", spec: DEFAULT_SPEC, windows: true, apim: other }),
    '$env:APIM_KEY = "<your-subscription-key>"\nbackend\\.venv\\Scripts\\python scripts\\benchmark.py --gateway apim --url https://apim.azure-api.net/gpt-4o --model gpt-4o --api-version 2024-10-21 --key-header api-key --users 2 --duration 30',
  )
})
