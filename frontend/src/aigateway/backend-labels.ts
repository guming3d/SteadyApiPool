import type { AigatewayMessages } from "../i18n"
import type { BackendView } from "./types"

export function authLabel(type: BackendView["auth_type"], t: AigatewayMessages) {
  switch (type) {
    case "api_key": return t.authApiKey
    case "bearer": return t.authBearer
    case "managed_identity": return t.authManagedIdentity
    default: return t.authCustom
  }
}

export function kindLabel(kind: BackendView["kind"], t: AigatewayMessages) {
  switch (kind) {
    case "foundry": return t.kindFoundry
    case "github": return t.kindGithub
    default: return t.kindCustom
  }
}
