import assert from "node:assert/strict"
import test from "node:test"

import { nextPreference, parsePreference, resolveMode } from "../src/theme.ts"

test("anything but an explicit light or dark choice follows the system", () => {
  assert.equal(parsePreference("light"), "light")
  assert.equal(parsePreference("dark"), "dark")
  for (const stored of [null, "", "system", "Dark", "auto"]) assert.equal(parsePreference(stored), "system", String(stored))
})

test("system resolves from the OS setting, explicit choices ignore it", () => {
  assert.equal(resolveMode("system", true), "dark")
  assert.equal(resolveMode("system", false), "light")
  assert.equal(resolveMode("light", true), "light")
  assert.equal(resolveMode("dark", false), "dark")
})

test("the toggle cycles through every choice and back", () => {
  assert.equal(nextPreference("system"), "light")
  assert.equal(nextPreference("light"), "dark")
  assert.equal(nextPreference("dark"), "system")
})
