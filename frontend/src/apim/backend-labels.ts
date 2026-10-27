import type { ApimMessages } from "../i18n"
import type { BackendView } from "./types"

type Messages = ApimMessages

export function authLabel(type: BackendView["auth_type"], t: Messages) {
  switch (type) {
    case "api_key": return t.authApiKey
    case "bearer": return t.authBearer
    case "managed_identity": return t.authManagedIdentity
    case "custom": return t.authCustom
    default: return t.authNone
  }
}

/** The breaker rule in one line, e.g. "1× / 60s → 60s". */
export function breakerLabel(backend: BackendView, t: Messages) {
  const breaker = backend.circuit_breaker
  if (breaker) return `${breaker.failure_count}× / ${breaker.interval_seconds}s → ${breaker.trip_duration_seconds}s`
  return backend.has_circuit_breaker ? t.authCustom : null
}
