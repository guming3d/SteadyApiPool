// Mirrors backend/pool_manager/models.py.

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

export type PoolMember = { backend_id: string; priority: number; weight: number }

export type PoolWrite = {
  title?: string | null
  description?: string | null
  members: PoolMember[]
  session_affinity: boolean
  /** The pool's rule; saving writes it to every member this app manages. Omitted: unchanged. */
  circuit_breaker?: CircuitBreaker | null
  etag?: string | null
}

/** Result of POST /api/pools/model-check; saving a pool enforces the same check. */
export type MemberModel = {
  backend_id: string
  /** ARM ID of the Foundry deployment; null when the member points elsewhere and is not checked. */
  deployment_id: string | null
  /** Read from the deployment resource, never inferred from a name. */
  model_name: string | null
  model_version: string | null
  /** Why a Foundry deployment's model could not be read; such a member fails the check. */
  problem: string | null
}

export type ModelCheck = { members: MemberModel[]; model_names: string[]; ok: boolean; message: string | null }

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

export type DeploymentView = {
  account_id: string
  account_name: string
  resource_group: string
  location: string
  deployment: string
  model_name: string
  model_version: string
  sku: string
  capacity: number | null
  backend_url: string
  local_auth_disabled: boolean
  suggested_backend_id: string
  backend_id: string | null
}

export type DeploymentRef = { account_id: string; deployment: string }

export type DeploymentImportResult = { backend_ids: string[]; created: string[] }

// -- creating Foundry deployments (mirrors pool_manager/models.py) --
/** Pay-per-token deployment types; provisioned (PTU) isn't offered. */
export type DeploymentSku = "GlobalStandard" | "DataZoneStandard" | "Standard"
export type VersionUpgrade = "OnceNewDefaultVersionAvailable" | "OnceCurrentVersionExpired" | "NoAutoUpgrade"

export type FoundryAccountView = {
  id: string
  name: string
  kind: string
  location: string
  resource_group: string
  subscription_id: string
}

/** Capacities are thousands of tokens per minute. */
export type ModelSkuOption = { name: DeploymentSku; default_capacity: number; min_capacity: number; max_capacity: number; step: number }

export type DeployableModel = {
  name: string
  version: string
  is_default_version: boolean
  lifecycle_status: string | null
  /** When inference stops, if announced (ISO 8601). */
  retires_on: string | null
  capabilities: string[]
  skus: ModelSkuOption[]
}

export type ModelCapacity = { available: Partial<Record<DeploymentSku, number>>; error: string | null }

export type FoundryDeploymentCreate = {
  account_id: string
  deployment: string
  model_name: string
  model_version: string
  sku: DeploymentSku
  capacity: number
  version_upgrade: VersionUpgrade
}

export type FoundryDeploymentStatus = { account_id: string; deployment: string; state: string; error: string | null }

export type Principal = {
  name: string
  can_write: boolean
  authenticated: boolean
  /** The public app: the Entra tenant the user signed in to. */
  tenant_id?: string | null
  /** The public app's read-only guest view: nothing can be changed or revealed. */
  guest?: boolean
}

/** GET /api/config, read before signing in. */
export type PublicConfig = {
  mode: "local" | "deployed" | "public"
  client_id: string | null
  authority: string | null
  api_scope: string | null
  consent_scopes: string[]
  /** Visitors may browse a demo service read-only without signing in. */
  guest: boolean
}

// -- setup wizard (mirrors pool_manager/models.py) --

export type SetupStatus = "ok" | "action" | "blocked" | "pending" | "unknown" | "skipped"
/** Fixes the app performs itself; anything else is an admin request. */
export type SetupFix = "sign_in" | "consent" | "refresh_token" | "select_apim" | "enable_apim_identity" | "grant_foundry_access"
export type SetupKind =
  | "sign_in" | "azure_token" | "apim_target" | "apim_access" | "discovery"
  | "app_apim" | "app_discovery" | "apim_identity" | "foundry_access"

export type AdminRequest = {
  assignee: string
  assignee_id: string | null
  role: string | null
  scope: string | null
  command: string
  portal_url: string | null
}

export type SetupCheck = {
  id: string
  kind: SetupKind
  /** Subscription or Foundry account ID the check is about. */
  subject: string | null
  status: SetupStatus
  missing: string[]
  detail: string | null
  fix: SetupFix | null
  request: AdminRequest | null
  granted_by: "owners" | "azure_rbac" | null
  /** When a pending operation began (ISO 8601, UTC). */
  started_at: string | null
}

export type SetupIdentity = { name: string | null; object_id: string | null; tenant_id: string | null }

export type SetupReport = {
  mode: "local" | "deployed" | "public" | "guest"
  user: SetupIdentity | null
  checks: SetupCheck[]
  /** Nothing needs action and nothing is pending. */
  ready: boolean
  /** The app can read the APIM service, so the rest of the UI works. */
  can_operate: boolean
  /** How the local app is signed in; null when deployed. */
  local_sign_in?: LocalSignIn | null
}

/** What a setup report is doing, in the order it does it; each step calls ARM. */
export type SetupStepKind =
  | "sign_in" | "apim_target" | "apim_access" | "app_apim" | "discovery"
  | "apim_identity" | "members" | "foundry_access"

export type SetupProgressStep = {
  kind: SetupStepKind
  state: "running" | "done"
  subject?: string | null
  /** Steps that check several subscriptions or accounts count them. */
  done?: number | null
  total?: number | null
}

/** How far the caller's setup report has got; empty once it has been sent. */
export type SetupProgress = {
  running: boolean
  steps: SetupProgressStep[]
}

export type SignInSource = "browser" | "device_code" | "azure_cli" | "azd" | "powershell"

export type LocalSignIn = {
  /** What answered the last token request; null when nothing is signed in. */
  source: SignInSource | null
  /** A sign-in made from the app that survives a restart. */
  remembered: boolean
  /** The OS can encrypt a token cache; without it app sign-ins last until the app stops. */
  persistent: boolean
  /** Sign-in tools on PATH. */
  tools: SignInSource[]
}

/** Enter user_code at verification_uri on any device to finish signing in. */
export type DeviceCodePrompt = { verification_uri: string; user_code: string; expires_on: number }

export type DeviceCodeStatus = { state: "idle" | "pending" | "done" | "failed"; error: string | null }

// -- choosing or creating the APIM service (mirrors pool_manager/models.py) --

export type TargetView = {
  subscription_id: string | null
  resource_group: string | null
  apim_name: string | null
  tenant_id: string | null
  /** "browser": the public app, where each user's browser remembers their own choice. */
  source: "environment" | "saved" | "browser" | "guest" | "none"
  /** Locally unless the environment fixes the service, and always in the public app; never in
   *  the Easy Auth deployment. */
  can_change: boolean
}

export type TenantView = { tenant_id: string; display_name: string | null; default_domain: string | null }

export type SubscriptionView = { subscription_id: string; display_name: string; tenant_id: string | null; state: string }

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

export type ResourceGroupView = { name: string; location: string }

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

export type ApiTestResult = {
  status: number
  elapsed_ms: number
  url: string
  headers: Record<string, string>
  body: string
  truncated: boolean
}

// -- pool dashboard (mirrors pool_manager/models.py) --

export type MetricsRange = "1h" | "6h" | "24h" | "7d"

export type TrafficPoint = { time: string; success: number; throttled: number; client_errors: number; server_errors: number }

export type LatencyPoint = { time: string; overall_ms: number | null; backend_ms: number | null }

/** What clients of the pool's APIs saw, from APIM's metrics. */
export type GatewayMetrics = {
  requests: number
  success: number
  /** 429s answered to clients: every member was throttled, even after retries. */
  throttled: number
  client_errors: number
  server_errors: number
  availability: number | null
  duration_ms: number | null
  backend_duration_ms: number | null
  traffic: TrafficPoint[]
  latency: LatencyPoint[]
}

/** A member's Foundry deployment, from its own metrics: all callers, not only this pool. */
export type MemberMetrics = {
  backend_id: string
  deployment_id: string | null
  region: string | null
  requests: number | null
  throttled: number | null
  server_errors: number | null
  tokens: number | null
  latency_ms: number | null
  utilization_pct: number | null
  expected_share: number
  error: string | null
}

export type PoolMetrics = {
  pool_id: string
  range: MetricsRange
  interval: string
  start: string
  end: string
  api_ids: string[]
  gateway: GatewayMetrics | null
  gateway_error: string | null
  members: MemberMetrics[]
}

// -- what the APIM service costs (mirrors pool_manager/models.py) --

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

/** This month so far, from Cost Management (it lags a few hours). */
export type MonthToDate = { amount: number; currency: string | null; since: string }

export type CostNote = "no_token" | "forbidden" | "unsupported" | "unavailable"

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
