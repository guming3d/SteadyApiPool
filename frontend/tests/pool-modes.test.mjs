import assert from "node:assert/strict"
import test from "node:test"

import {
  addMember,
  appendMembers,
  changeWeight,
  deploymentMode,
  draftIssues,
  membersForMode,
  MODEL_NAME_PATTERN,
  POOL_ID_PATTERN,
  primaryFirst,
  removeMember,
  setPrimary,
  suggestPoolId,
  trafficShare,
} from "../src/pool-modes.ts"

const member = (backend_id, priority = 0, weight = 1) => ({ backend_id, priority, weight })

test("modes are derived from member priorities and weights", () => {
  assert.equal(deploymentMode([member("a")]), "failover")
  assert.equal(deploymentMode([member("a", 0), member("b", 1), member("c", 1)]), "failover")
  assert.equal(deploymentMode([member("a"), member("b")]), "balanced")
  assert.equal(deploymentMode([member("a", 0, 2), member("b", 0, 1)]), "weighted")
  assert.equal(deploymentMode([member("a", 0), member("b", 1), member("c", 2)]), "custom")
  assert.equal(deploymentMode([member("a", 1), member("b", 0)]), "custom")
})

test("switching modes rewrites priorities and weights around the primary", () => {
  const members = [member("a"), member("b"), member("c")]
  assert.deepEqual(membersForMode(members, "failover"), [member("a", 0), member("b", 1), member("c", 1)])
  assert.deepEqual(membersForMode(members, "weighted"), [member("a", 0, 2), member("b"), member("c")])
  const custom = [member("a", 0, 5), member("b", 0, 3)]
  assert.deepEqual(membersForMode(custom, "weighted"), custom)
  assert.deepEqual(membersForMode(custom, "balanced"), [member("a"), member("b")])
})

test("the first member added is primary and later failover members are standbys", () => {
  let members = addMember([], "a", null)
  members = addMember(members, "b", "failover")
  assert.deepEqual(members, [member("a", 0), member("b", 1)])
  assert.deepEqual(addMember([member("a")], "b", "balanced"), [member("a"), member("b")])
})

test("appending deployments to an existing pool keeps its mode and skips current members", () => {
  const failover = [member("a", 0), member("b", 1)]
  assert.deepEqual(appendMembers(failover, ["B", "c", "c"]), {
    members: [member("a", 0), member("b", 1), member("c", 1)],
    added: ["c"],
  })
  assert.deepEqual(appendMembers([member("a"), member("b")], ["c"]).members, [member("a"), member("b"), member("c")])
  const weighted = appendMembers([member("a", 0, 3), member("b", 0, 1)], ["c"]).members
  assert.equal(deploymentMode(weighted), "weighted")
  assert.deepEqual(appendMembers(failover, ["a"]), { members: failover, added: [] })
  assert.deepEqual(appendMembers([], ["a", "b"]).members, [member("a", 0), member("b", 1)])
})

test("removing or changing the primary keeps failover valid", () => {
  const failover = [member("a", 0), member("b", 1), member("c", 1)]
  assert.deepEqual(removeMember(failover, "a", "failover"), [member("b", 0), member("c", 1)])
  assert.deepEqual(setPrimary(failover, "c", "failover"), [member("c", 0), member("a", 1), member("b", 1)])
  assert.equal(deploymentMode(setPrimary(failover, "c", "failover")), "failover")
})

test("weights clamp to 1..100 and shares round", () => {
  const members = changeWeight([member("a"), member("b")], "a", 500)
  assert.equal(members[0].weight, 100)
  assert.equal(changeWeight(members, "a", 0)[0].weight, 1)
  assert.equal(trafficShare([member("a", 0, 3), member("b", 0, 1)], "a"), 75)
})

test("drafts need 2-30 members and distinct weights in weighted mode", () => {
  assert.deepEqual(draftIssues({ members: [member("a")] }, "failover"), ["members"])
  assert.deepEqual(draftIssues({ members: [member("a"), member("b")] }, "weighted"), ["weights"])
  assert.deepEqual(draftIssues({ members: [member("a", 0, 2), member("b")] }, "weighted"), [])
})

test("reloaded pools keep the written primary when priorities tie", () => {
  assert.deepEqual(primaryFirst([member("a", 0, 2), member("b", 0, 3)]).map((m) => m.backend_id), ["a", "b"])
  assert.deepEqual(primaryFirst([member("b", 1), member("a", 0)]).map((m) => m.backend_id), ["a", "b"])
})

test("a new pool's ID comes from its model and is a valid model name", () => {
  assert.equal(suggestPoolId("gpt-4.1"), "gpt-4-1")
  assert.equal(suggestPoolId("  GPT 4o Mini! "), "gpt-4o-mini")
  assert.equal(suggestPoolId(null), "pool")
  assert.equal(suggestPoolId("gpt-4o", ["GPT-4o", "gpt-4o-2"]), "gpt-4o-3")
  for (const id of ["gpt-4-1", "gpt-4o-mini", "pool"]) {
    assert.match(id, POOL_ID_PATTERN)
    assert.match(id, MODEL_NAME_PATTERN)
  }
  assert.match("gpt-4o-2024-11-20", MODEL_NAME_PATTERN)
  assert.match("azure:gpt-4.1", MODEL_NAME_PATTERN)
  assert.doesNotMatch("gpt 4o", MODEL_NAME_PATTERN)
  assert.doesNotMatch("gpt.4", POOL_ID_PATTERN)
})
