import type { LitellmMessages } from "../i18n"
import type { BackendView } from "./types"

type Messages = LitellmMessages

export function authLabel(type: BackendView["auth_type"], t: Messages) {
  switch (type) {
    case "api_key": return t.authApiKey
    case "managed_identity": return t.authManagedIdentity
    case "custom": return t.authCustom
    default: return t.authNone
  }
}

/** What LiteLLM calls the member as, e.g. "azure/gpt-4o" on its endpoint. */
export const targetLabel = (backend: BackendView) => `${backend.provider}/${backend.model}`
