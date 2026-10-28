// Mirrors backend/pool_manager/models.py: what both gateways share. Each product's own types
// are in apim/types.ts and litellm/types.ts.

/** The gateway product the app manages. */
export type Product = "apim" | "litellm"

export type PoolMember = { backend_id: string; priority: number; weight: number }

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
  /** The deployment's type, capacity units, and the per-minute limits Azure enforces on it. */
  deployment_type: string | null
  capacity: number | null
  tpm: number | null
  rpm: number | null
  /** rate_limits: Azure's own; capacity: estimated at an older chat model's 1,000 TPM, 6 RPM a unit. */
  quota_source: "rate_limits" | "capacity" | null
}

export type ModelCheck = {
  members: MemberModel[]
  model_names: string[]
  ok: boolean
  message: string | null
  /** What the pool's deployments allow together, each deployment once; null if none could be read. */
  total_tpm: number | null
  total_rpm: number | null
}

/** LiteLLM: a Container App, or any other LiteLLM proxy connected by URL (e.g. on AKS). */
export type GatewayKind = "aca" | "external"

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
  /** Member already in a pool pointing at this deployment. */
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
  /** Visitors may browse a demo gateway read-only without signing in. */
  guest: boolean
}

// -- setup wizard (mirrors pool_manager/models.py) --

export type SetupStatus = "ok" | "action" | "blocked" | "pending" | "unknown" | "skipped"

/** Fixes the app performs itself; anything else is an admin request. */
export type SetupFix =
  | "sign_in" | "consent" | "refresh_token" | "select_gateway"
  // APIM: turn on the service's identity. LiteLLM: an external gateway's master key is unknown.
  | "enable_apim_identity" | "connect_gateway"
  | "grant_foundry_access"

export type SetupKind =
  | "sign_in" | "azure_token" | "gateway_target" | "gateway_access" | "gateway_health"
  | "discovery" | "app_gateway" | "app_discovery" | "gateway_identity" | "foundry_access"

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
  /** Subscription, Foundry account, gateway or identity the check is about. */
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
  /** The kind of gateway chosen; null until one is. */
  product: Product | null
  user: SetupIdentity | null
  checks: SetupCheck[]
  /** Nothing needs action and nothing is pending. */
  ready: boolean
  /** The app can work with the gateway, so the rest of the UI works. */
  can_operate: boolean
  /** How the local app is signed in; null when deployed. */
  local_sign_in?: LocalSignIn | null
}

/** What a setup report is doing, in the order it does it. */
export type SetupStepKind =
  | "sign_in" | "gateway_target" | "gateway_access" | "app_gateway" | "gateway_health"
  | "discovery" | "gateway_identity" | "members" | "foundry_access"

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

// -- choosing, deploying or connecting the gateway (mirrors pool_manager/models.py) --

export type TargetView = {
  product: Product | null
  /** The APIM service's or the LiteLLM gateway's name. */
  gateway_name: string | null
  gateway_kind: GatewayKind | null
  subscription_id: string | null
  resource_group: string | null
  /** External LiteLLM gateways only. */
  gateway_url: string | null
  tenant_id: string | null
  /** "browser": the public app, where each user's browser remembers their own choice. */
  source: "environment" | "saved" | "browser" | "guest" | "none"
  /** Locally unless the environment fixes the gateway, and always in the public app; never in
   *  the Easy Auth deployment. */
  can_change: boolean
  /** LiteLLM: an AKS gateway the app deployed, and whether the app is still installing it. */
  cluster_id: string | null
  provisioning: boolean
}

export type TenantView = { tenant_id: string; display_name: string | null; default_domain: string | null }

export type SubscriptionView = { subscription_id: string; display_name: string; tenant_id: string | null; state: string }

export type ResourceGroupView = { name: string; location: string }

export type ApiTestResult = {
  status: number
  elapsed_ms: number
  url: string
  headers: Record<string, string>
  body: string
  truncated: boolean
}

// -- load test (mirrors pool_manager/models.py) --

export type MetricsRange = "1h" | "6h" | "24h" | "7d"

export type TrafficPoint = { time: string; success: number; throttled: number; client_errors: number; server_errors: number }

export type LatencyPoint = { time: string; overall_ms: number | null; backend_ms: number | null }

/** What clients of the pool saw, from the gateway's spans in Application Insights. */
export type GatewayMetrics = {
  requests: number
  success: number
  /** 429s answered to clients: every member was throttled or cooling down. */
  throttled: number
  client_errors: number
  server_errors: number
  /** LiteLLM: requests that succeeded only because it retried on another member. APIM's
   *  metrics can't tell, so it has none. */
  failovers?: number | null
  availability: number | null
  duration_ms: number | null
  backend_duration_ms: number | null
  traffic: TrafficPoint[]
  latency: LatencyPoint[]
}

/** A member: the gateway's attempts on it, and its Foundry deployment's own metrics. */
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
  gateway_attempts?: number | null
  gateway_failures?: number | null
  gateway_throttled?: number | null
}

export type PoolMetrics = {
  pool_id: string
  range: MetricsRange
  interval: string
  start: string
  end: string
  /** What clients reach the pool by: the APIs bound to it (APIM; none means no gateway
   *  numbers yet), or the pool ID and its aliases (LiteLLM). */
  names: string[]
  gateway: GatewayMetrics | null
  /** "no_app_insights" or "forbidden" (LiteLLM), or Azure's message. */
  gateway_error: string | null
  members: MemberMetrics[]
}

// -- what the gateway costs (mirrors pool_manager/models.py) --

/** This month so far, from Cost Management (it lags a few hours). */
export type MonthToDate = { amount: number; currency: string | null; since: string }

export type CostNote = "no_token" | "forbidden" | "unsupported" | "unavailable" | "external"
