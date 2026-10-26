import assert from "node:assert/strict"
import test from "node:test"

import { displayUser } from "../src/identity.ts"

const local = { name: "local-developer", can_write: true, authenticated: false }
const azure = { name: "dev@contoso.com", object_id: "oid-1", tenant_id: "tid-1" }

test("a page sign-in names the signed-in user, whatever Azure account the app uses", () => {
  const me = { name: "ops@contoso.com", can_write: false, authenticated: true }
  assert.deepEqual(displayUser(me, azure), { kind: "user", name: "ops@contoso.com" })
})

test("locally the placeholder principal gives way to the Azure account", () => {
  assert.deepEqual(displayUser(local, azure), { kind: "local", name: "dev@contoso.com" })
  // A service principal's token has no user name, only an object ID.
  assert.deepEqual(displayUser(local, { name: null, object_id: "oid-2", tenant_id: null }), { kind: "local", name: "oid-2" })
})

test("locally without an Azure sign-in it says so instead of naming anyone", () => {
  assert.deepEqual(displayUser(local, null), { kind: "local-none" })
  assert.deepEqual(displayUser(local, undefined), { kind: "local-none" })
  assert.deepEqual(displayUser(local, { name: null, object_id: null, tenant_id: null }), { kind: "local-none" })
})

test("the public app's guest view is named as such, never as an account", () => {
  const guest = { name: "guest", can_write: false, authenticated: false, guest: true }
  assert.deepEqual(displayUser(guest, azure), { kind: "guest" })
})
