// Mirrors backend/pool_manager/apim/models.py.

import type { CostNote, MonthToDate, PoolMember, ResourceGroupView } from "../types"

export type FailureStatusCodeRange = { minimum: number; maximum: number }

export type ErrorReason = "BackendConnectionFailure" | "Timeout" | "ClientConnectionFailure"

export type CircuitBreaker = {
  failure_count: number
  interval_seconds: number
  trip_duration_seconds: number
  accept_retry_after: boolean
  status_code_ranges: FailureStatusCodeRange[]
  error_reasons: ErrorReason[]
}

export type AuthType = "none" | "api_key" | "bearer" | "managed_identity"

export type SecretSource =
  | { value: string }
  | { key_vault_secret_id: string; key_vault_identity_client_id?: string | null }

export type MemberBackendWrite = {
  title?: string | null
  url: string
  auth: { type: AuthType; header_name?: string; secret?: SecretSource | null }
  circuit_breaker: CircuitBreaker | null
  etag?: string | null
}

export type BackendView = {
  id: string
  title: string | null
  description: string | null
  type: "Single" | "Pool"
  url: string | null
  auth_type: AuthType | "custom"
  header_name: string | null
  named_value: string | null
  circuit_breaker: CircuitBreaker | null
  has_circuit_breaker: boolean
  managed: boolean
  protected: boolean
  etag: string | null
}

export type PoolWrite = {
  title?: string | null
  description?: string | null
  members: PoolMember[]
  session_affinity: boolean
  /** The pool's rule; saving writes it to every member this app manages. Omitted: unchanged. */
  circuit_breaker?: CircuitBreaker | null
  etag?: string | null
}

export type PoolView = {
  id: string
  title: string | null
  description: string | null
  members: PoolMember[]
  session_affinity: boolean
  session_cookie_name: string | null
  managed: boolean
  protected: boolean
  etag: string | null
}

export type ApiBindingWrite = {
  pool_id: string
  retry_on_429: boolean
  max_attempts_per_request: number
  retry_interval_seconds: number
  first_fast_retry: boolean
  backend_timeout_seconds: number
  managed_identity_resource: string | null
  etag?: string | null
}

export type ApiBinding = {
  pool_id: string
  retry_on_429: boolean
  /** Read back from the managed retry block; null when 429 retry is off. */
  max_attempts_per_request: number | null
  retry_interval_seconds: number | null
  first_fast_retry: boolean | null
  backend_timeout_seconds: number | null
  managed_identity_resource: string | null
}

export type ApiView = {
  id: string
  display_name: string
  path: string
  binding: ApiBinding | null
  foreign_backend_selection: boolean
  /** Created by this app. */
  managed: boolean
  /** Clients authenticate with an APIM subscription key sent in this header. */
  subscription_required: boolean
  subscription_key_header: string
  etag: string | null
}

// -- removing pools with their APIs, and cleaning up a service (mirrors pool_manager/models.py) --

export type RemovalApi = {
  id: string
  display_name: string
  path: string
  /** The pool it is bound to; null for an API this app created that is no longer bound. */
  pool_id: string | null
}

/** What removing pools takes with it; nothing is written until it is confirmed. */
export type RemovalPlan = {
  pools: string[]
  /** APIs this app created for those pools, deleted along with their policy. */
  delete_apis: RemovalApi[]
  /** Other APIs bound to them; unbinding restores their original policy. */
  unbind_apis: RemovalApi[]
  /** Members this app created that no remaining pool uses. */
  delete_members: string[]
  /** Cleanup only: pools this app didn't create, left alone. */
  kept_pools: string[]
  /** Why the plan can't run. */
  blocked: string[]
}

/** The plan the user reviewed; the backend refuses to remove anything not listed. */
export type RemovalConfirm = {
  pools: string[]
  delete_apis: string[]
  unbind_apis: string[]
  etag?: string | null
}

export type CleanupConfirm = RemovalConfirm & { confirm_name: string }

export type RemovalResult = {
  pools: string[]
  deleted_apis: string[]
  unbound_apis: string[]
  deleted_members: string[]
}

/** POST /api/pools/{id}/api: creates an HTTP API for the pool and binds it. */
export type PoolApiCreate = { api_id?: string | null; path: string; display_name?: string | null }

export type PolicyView = {
  api_id: string
  /** null when the API has no API-level policy. */
  xml: string | null
  etag: string | null
}

export type PolicyPreview = { current: string | null; proposed: string }

export type Capabilities = {
  apim_name: string
  resource_group: string
  session_affinity_supported: boolean
  id_prefix: string
  protected_prefixes: string[]
  /** ARM ID of the APIM service, for links to the Azure portal. */
  apim_resource_id: string
  apim_principal_id: string | null
  /** Base URL clients call, e.g. https://<apim>.azure-api.net. */
  gateway_url: string | null
  discovery_subscriptions: string[]
}

export type ApimServiceView = {
  id: string
  name: string
  resource_group: string
  location: string
  sku: string
  provisioning_state: string | null
  /** Created by this app. */
  managed: boolean
  /** The signed-in user may do every APIM read and write the app makes; null if unknown. */
  can_manage: boolean | null
  missing: string[]
}

export type ApimSku = "BasicV2" | "StandardV2" | "Developer"

export type ApimCreateOptions = {
  resource_groups: ResourceGroupView[]
  /** Regions offering each tier in this subscription. */
  locations: Record<ApimSku, string[]>
  provider_registered: boolean
}

export type TargetSelect = { tenant_id: string | null; subscription_id: string; resource_group: string; apim_name: string }

export type ApimCreate = {
  tenant_id: string | null
  subscription_id: string
  resource_group: string
  location: string
  name: string
  sku: ApimSku
  publisher_email: string
  publisher_name: string
}

// -- trying a pool API (mirrors pool_manager/models.py) --

export type KeyScope = "api" | "product" | "all_apis"

/** An APIM subscription whose key opens an API; the key is read separately. */
export type SubscriptionKeyView = {
  id: string
  display_name: string
  scope: KeyScope
  product: string | null
  state: string
  /** The test key this app creates for the API. */
  managed: boolean
}

export type ApiTestRequest = {
  path: string
  api_version: string | null
  subscription_key: string | null
  body: Record<string, unknown>
}

/** The tier's retail price, before discounts and tax. */
export type ListPrice = {
  /** As Azure prices it, e.g. "Basic v2". */
  tier: string
  units: number
  /** Per unit; null for tiers billed only per call. */
  hourly: number | null
  /** All units over an average month of 730 hours. */
  monthly: number | null
  /** Requests beyond what the tier includes. */
  per_10k_calls: number | null
  currency: string
}

export type ServiceCost = {
  sku: string
  units: number
  location: string
  list_price: ListPrice | null
  month_to_date: MonthToDate | null
  month_to_date_note: CostNote | null
  /** Azure's own message when month_to_date failed. */
  detail: string | null
  portal_url: string
}

// -- deleting and restoring an APIM service (mirrors pool_manager/models.py) --

/** What deleting a service would remove, shown before the user confirms. */
export type ServiceDeleteCheck = {
  name: string
  resource_group: string
  location: string
  sku: string
  /** The service the app manages now. */
  current: boolean
  pools: number
  members: number
  apis: number
}

/** A soft-deleted service Azure still keeps (48 hours) and can restore. */
export type DeletedServiceView = {
  name: string
  location: string
  display_location: string
  resource_group: string
  service_id: string
  deletion_date: string | null
  scheduled_purge_date: string | null
}
