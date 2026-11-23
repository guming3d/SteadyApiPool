// Mirrors backend/pool_manager/aigateway/models.py.

import type { BenchmarkSpec, CostNote, MonthToDate, PoolMember, ResourceGroupView } from "../types"

export type FailureStatusCodeRange = { minimum: number; maximum: number }

/** The pool's failure rule, written to its alias: a member that answers one of these statuses
 *  (or doesn't answer) is tried same_member_retries more times, then the request moves on and
 *  the member stays out of the pool for exclude_seconds. */
export type CircuitBreaker = {
  status_code_ranges: FailureStatusCodeRange[]
  /** Timeouts and refused connections: the gateway got no HTTP answer. */
  no_response: boolean
  /** 0 skips the member for the failed request only. */
  exclude_seconds: number
  same_member_retries: number
}

export type AuthType = "api_key" | "bearer"

/** A member that isn't a Foundry deployment: an OpenAI-compatible base URL and its model. */
export type MemberBackendWrite = {
  title?: string | null
  url: string
  model: string
  /** On update, omitting the secret keeps the one the gateway has. */
  auth: { type: AuthType; header_name?: string; secret?: string | null }
  etag?: string | null
}

export type MemberKind = "foundry" | "custom" | "github"

/** A member: a model registered in the gateway, under its model provider. */
export type BackendView = {
  id: string
  title: string | null
  /** ARM ID of the registration, which aliases point at. */
  resource_id: string
  provider: string
  kind: MemberKind
  /** Foundry: the deployment's URL; custom: the endpoint's base URL. */
  url: string | null
  /** The model (Foundry: the deployment) the gateway asks the provider for. */
  model: string
  deployment_id: string | null
  auth_type: "managed_identity" | AuthType | "custom"
  header_name: string | null
  pools: string[]
  managed: boolean
  protected: boolean
  etag: string | null
}

export type PoolWrite = {
  title?: string | null
  description?: string | null
  members: PoolMember[]
  /** Written to the alias. Omitted: what the pool has (the defaults when new). */
  circuit_breaker?: CircuitBreaker | null
  /** Extra sends of one request across the pool's members (maxRetryAttempts). */
  retries?: number | null
  etag?: string | null
}

export type PoolView = {
  id: string
  /** The name clients send as model (the alias's routing name). */
  model_name: string
  title: string | null
  description: string | null
  members: PoolMember[]
  circuit_breaker: CircuitBreaker | null
  /** Failure rules this app didn't write; saving replaces them with the pool's. */
  custom_failures: boolean
  retries: number | null
  /** Inline policies on the alias (token and cost limits, ...), kept as they are on a save. */
  policies: string[]
  managed: boolean
  protected: boolean
  etag: string | null
}

/** How clients call a pool: the gateway's OpenAI v1 base URL and the pool's model name. */
export type PoolEndpoint = {
  pool_id: string
  /** https://<host>/default/models/openai/v1; null while the gateway has no host name. */
  base_url: string | null
  model_name: string
}

export type Capabilities = {
  gateway_name: string
  resource_group: string
  id_prefix: string
  protected_prefixes: string[]
  gateway_resource_id: string
  gateway_principal_id: string | null
  gateway_url: string | null
  location: string | null
  sku: string | null
  /** Application Insights component its telemetry exporter sends to. */
  app_insights_id: string | null
  discovery_subscriptions: string[]
}

// -- removal --

export type RemovalPlan = {
  pools: string[]
  /** Members this app created that no remaining pool uses, with their provider once empty. */
  delete_members: string[]
  /** Cleanup only: the test key, which opens every alias on the gateway. */
  delete_keys: string[]
  /** Cleanup only: pools this app didn't create, left alone. */
  kept_pools: string[]
  blocked: string[]
}

export type RemovalConfirm = {
  pools: string[]
  delete_keys: string[]
  etag?: string | null
}

export type CleanupConfirm = RemovalConfirm & { confirm_name: string }

export type RemovalResult = {
  pools: string[]
  deleted_members: string[]
  deleted_keys: string[]
}

// -- cost --

/** AI Gateway is billed per request, not per hour. */
export type ListPrice = {
  /** Per 10,000 requests; 0 while Azure lists no charge. */
  per_10k_requests: number
  currency: string
}

export type ServiceCost = {
  sku: string | null
  location: string | null
  list_price: ListPrice | null
  month_to_date: MonthToDate | null
  month_to_date_note: CostNote | null
  detail: string | null
  portal_url: string
}

// -- choosing, creating and deleting a gateway --

export type GatewayView = {
  id: string
  name: string
  resource_group: string
  location: string
  sku: string
  url: string | null
  provisioning_state: string | null
  managed: boolean
  can_manage: boolean | null
  missing: string[]
}

export type GatewayCreateOptions = {
  resource_groups: ResourceGroupView[]
  locations: string[]
  provider_registered: boolean
}

export type TargetSelect = {
  tenant_id?: string | null
  subscription_id: string
  resource_group: string
  aigateway_name: string
  force?: boolean
}

export type GatewayCreate = {
  tenant_id?: string | null
  subscription_id: string
  resource_group: string
  location: string
  name: string
}

export type GatewayDeleteCheck = {
  name: string
  resource_group: string
  location: string
  sku: string
  current: boolean
  pools: number
  members: number
  keys: number
}

// -- trying a pool --

/** A runtime API key. Every key opens every alias on the gateway, not only one pool. */
export type ApiKeyView = {
  id: string
  display_name: string | null
  state: string | null
  expires: string | null
  /** The test key this app creates. */
  managed: boolean
}

export type ApiKeySecret = { primary_key: string }

export type TestPath = "chat/completions" | "responses" | "embeddings"

export type ApiTestRequest = {
  path: TestPath
  body: Record<string, unknown>
  key: string | null
}

export type BenchmarkStart = { spec: BenchmarkSpec; key: string }
