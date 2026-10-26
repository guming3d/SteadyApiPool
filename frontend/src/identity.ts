// Who the top bar names. Pure, so node --test can import it; tui/pool_tui/logic.py ports it.
import type { Principal, SetupIdentity } from "./types"

export type DisplayUser =
  // Signed in to this page (Easy Auth when deployed).
  | { kind: "user"; name: string }
  // Page sign-in is off (local dev), so the principal is a placeholder; name the Azure
  // account the app calls Azure with instead.
  | { kind: "local"; name: string }
  // Local and not signed in to Azure yet.
  | { kind: "local-none" }
  // The public app's read-only guest view.
  | { kind: "guest" }

export function displayUser(me: Principal, azure: SetupIdentity | null | undefined): DisplayUser {
  if (me.guest) return { kind: "guest" }
  if (me.authenticated) return { kind: "user", name: me.name }
  const name = azure?.name || azure?.object_id
  return name ? { kind: "local", name } : { kind: "local-none" }
}
