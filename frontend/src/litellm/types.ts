// Mirrors backend/pool_manager/litellm/models.py.

import type { BenchmarkSpec, CostNote, GatewayKind, MonthToDate, PoolMember, ResourceGroupView } from "../types"

/** LiteLLM's cooldown for each member: a member that fails more than allowed_fails times in a
 *  minute sits out cooldown_seconds, and its share goes to the others (or the next priority). */
export type CircuitBreaker = {
  allowed_fails: number
  cooldown_seconds: number
}

export type AuthType = "none" | "api_key" | "managed_identity"

/** An Azure OpenAI deployment URL (.../openai/deployments/<name>), or an OpenAI-compatible
 *  base URL with the model it serves. */
export type MemberBackendWrite = {
  title?: string | null
  url: string
  model?: string | null
  /** On update, an api_key member without a secret keeps the key the gateway has. */
  auth: { type: AuthType; secret?: string | null }
}

export type BackendView = {
  id: string
  title: string | null
  url: string
  provider: "azure" | "openai"
  /** The deployment for Azure members; the model asked for on OpenAI-compatible ones. */
  model: string
  auth_type: AuthType | "custom"
  /** Pools it is a member of; empty until a pool using it is saved. */
  pools: string[]
  managed: boolean
  protected: boolean
}

export type PoolWrite = {
  title?: string | null
  description?: string | null
  members: PoolMember[]
  /** Written to every member row. Omitted: what the pool has (the defaults when new). */
  circuit_breaker?: CircuitBreaker | null
  /** Further attempts LiteLLM makes for a failed request, on the same or another member. */
  retries?: number | null
  /** How long one attempt may take before LiteLLM gives up on that member. */
  timeout_seconds?: number | null
  etag?: string | null
}

export type PoolView = {
  id: string
  title: string | null
  description: string | null
  members: PoolMember[]
  circuit_breaker: CircuitBreaker | null
  retries: number | null
  timeout_seconds: number | null
  /** Other names clients may send for this pool. */
  aliases: string[]
  managed: boolean
  protected: boolean
  /** A hash of the pool's rows: LiteLLM has no ETags. */
  etag: string | null
}

export type AliasView = { name: string; pool_id: string; managed: boolean }

/** How clients call a pool: the gateway's OpenAI-compatible base URL and a model name. */
export type PoolEndpoint = {
  pool_id: string
  /** <gateway>/v1; null while the gateway has no URL. */
  base_url: string | null
  /** The pool ID first, then its aliases. */
  model_names: string[]
  aliases: AliasView[]
}

/** The pool as LiteLLM config.yaml describes it; member secrets are left out. */
export type ConfigView = { pool_id: string; yaml: string | null }

export type ConfigPreview = { current: string | null; proposed: string }

// -- removing pools, and cleaning up the gateway (mirrors pool_manager/models.py) --

export type RemovalAlias = { name: string; pool_id: string }

export type RemovalKey = { id: string; alias: string | null; pool_id: string | null }

/** What removing pools takes with it; nothing is written until it is confirmed. */
export type RemovalPlan = {
  pools: string[]
  /** Aliases this app added for those pools. */
  delete_aliases: RemovalAlias[]
  /** Other aliases pointing at them; removed too, so none is left dangling. */
  unbind_aliases: RemovalAlias[]
  /** Test keys this app created for those pools. */
  delete_keys: RemovalKey[]
  /** Cleanup only: pools this app didn't create, left alone. */
  kept_pools: string[]
  /** Why the plan can't run. */
  blocked: string[]
}

/** The plan the user reviewed; the backend refuses to remove anything not listed. */
export type RemovalConfirm = {
  pools: string[]
  delete_aliases: string[]
  unbind_aliases: string[]
  delete_keys: string[]
  etag?: string | null
}

export type CleanupConfirm = RemovalConfirm & { confirm_name: string }

export type RemovalResult = {
  pools: string[]
  deleted_aliases: string[]
  unbound_aliases: string[]
  deleted_keys: string[]
}

export type Capabilities = {
  gateway_name: string
  /** "aca": a Container App; "external": a LiteLLM proxy connected by URL, e.g. on AKS. */
  gateway_kind: GatewayKind
  resource_group: string | null
  id_prefix: string
  protected_prefixes: string[]
  /** ARM ID of the Container App, for links to the Azure portal. */
  gateway_resource_id: string | null
  /** The identity the gateway calls managed-identity members as. */
  gateway_principal_id: string | null
  /** Base URL of the gateway, e.g. https://<app>.<env>.<region>.azurecontainerapps.io. */
  gateway_url: string | null
  litellm_version: string | null
  /** The traffic the gateway was deployed or resized for; null before the sizer. */
  sized_for: GatewaySizing | null
  /** Application Insights component that receives the gateway's spans. */
  app_insights_id: string | null
  discovery_subscriptions: string[]
}

/** A label from the sizing plan: "dev" for a single replica, else "production". */
export type GatewayProfile = "dev" | "production"

/** A LiteLLM gateway on Container Apps. */
export type GatewayView = {
  id: string
  name: string
  resource_group: string
  location: string
  url: string | null
  profile: GatewayProfile | null
  provisioning_state: string | null
  /** Deployed by this app. */
  managed: boolean
  /** The signed-in user may read the app and its master key; null if unknown. */
  can_manage: boolean | null
  missing: string[]
  /** aks: an AKS cluster the app deployed LiteLLM on (id is the cluster's). */
  platform: GatewayPlatform
}

export type GatewayCreateOptions = {
  resource_groups: ResourceGroupView[]
  /** Regions where the subscription can create Container Apps environments. */
  locations: string[]
  /** Providers the template needs that aren't registered; deploying registers them. */
  unregistered_providers: string[]
}

export type TargetSelect = { tenant_id: string | null; subscription_id: string; resource_group: string; gateway_name: string; force?: boolean }

export type ModelSpeed = "fast" | "standard" | "reasoning"

/** The traffic a gateway is sized for (backend litellm/sizing.py): its peak. */
export type GatewaySizing = {
  peak_rpm: number
  input_tokens: number
  output_tokens: number
  /** The longest a call may take: LiteLLM's request_timeout. */
  timeout_seconds: number
  streaming: boolean
  model_speed: ModelSpeed
  /** Overrides the speed preset: average seconds a call takes. */
  response_seconds: number | null
  headroom_percent: number
  /** One more replica than the peak needs, at least two, zones for AKS nodes. */
  zone_redundant: boolean
  kubernetes_team: boolean
  self_hosted_models: boolean
  private_network: boolean
}

export type PlanNoteCode =
  | "platform_simple" | "platform_team" | "platform_gpu" | "platform_timeout" | "platform_scale"
  | "platform_chosen" | "private_aks" | "private_manual" | "call_exceeds_timeout" | "single_replica"
  | "redis_buffer" | "foundry_quota" | "outbound_capped" | "pool_lowered" | "aca_timeout"
  | "aca_replicas" | "aks_too_big" | "database_too_small"

export type PlanNote = { code: PlanNoteCode; values: Record<string, number | string> }

export type AksPlan = {
  node_size: string
  node_vcpus: number
  pods_per_node: number
  node_min: number
  node_max: number
  tier: "Free" | "Standard"
  outbound_ips: number
  outbound_ports: number
  ports_needed: number
}

/** What the sizer derives for one platform, with every number it used. */
export type GatewayPlan = {
  platform: GatewayPlatform
  profile: GatewayProfile
  rps: number
  tpm: number
  call_seconds: number
  in_flight: number
  in_flight_slow: number
  cpu_ms: number
  replica_rps: number
  replica_in_flight: number
  bound_by: "cpu" | "in_flight"
  peak_replicas: number
  min_replicas: number
  max_replicas: number
  scale_concurrency: number
  scale_cpu_percent: number
  replica_cpu: number
  replica_memory_gib: number
  bandwidth_mbps: number
  postgres_sku: string
  postgres_tier: "Burstable" | "GeneralPurpose"
  postgres_connections: number
  postgres_limit: number
  /** A standby server in another zone (zone-redundant high availability). */
  postgres_ha: boolean
  db_pool: number
  redis_sku: string | null
  redis_buffer: boolean
  aks: AksPlan | null
  /** Spread over availability zones: the Container Apps environment or the AKS nodes. */
  zone_redundant: boolean
  request_timeout: number
  route_timeout: number
  /** Seconds a stopping replica waits for its calls in flight: a call and its retry. */
  drain_seconds: number
  reasons: PlanNote[]
  warnings: PlanNote[]
  /** Anything here keeps the plan from being deployed on this platform. */
  problems: PlanNote[]
}

export type GatewayPlans = { recommended: GatewayPlatform; aca: GatewayPlan; aks: GatewayPlan }

export type ResizeItem =
  | "replicas" | "scale_rule" | "replica_size" | "nodes" | "pods" | "database" | "node_size"
  | "outbound" | "tier" | "redis" | "timeout" | "zones" | "database_ha"

/** apply: the app changes it; optional: only when asked; manual: the user does (command, if any). */
export type ResizeChange = {
  item: ResizeItem
  current: string
  proposed: string
  action: "apply" | "optional" | "manual"
  command: string | null
}

/** What resizing the gateway for a scenario would change; read without changing anything. */
export type ResizePlan = {
  platform: GatewayPlatform
  plan: GatewayPlan
  changes: ResizeChange[]
  /** The gateway as it was read; applying refuses one that changed since. */
  etag: string
  sized_for: GatewaySizing | null
  problems: string[]
}

export type ResizeApply = { sizing: GatewaySizing; etag: string; database: boolean }
export type ResizeResult = { applied: ResizeItem[]; command_id: string | null }
export type ResizeCommand = { state: "running" | "succeeded" | "failed"; exit_code: number | null; log_tail: string | null }

export type GatewayCreate = {
  tenant_id: string | null
  subscription_id: string
  resource_group: string
  location: string
  name: string
  sizing: GatewaySizing
  platform: GatewayPlatform
  /** When a resource fails: stop the deployment and delete what it created. */
  cleanup_on_failure: boolean
}

/** What the deployment would create there and cost, and what would keep it from succeeding. */
export type GatewayPreflight = {
  location: string
  plan: GatewayPlan
  /** null when the Retail Prices API is unreachable. */
  list_price: ListPrice | null
  problems: string[]
  /** Common regions where nothing is in the way; only looked up when there are problems. */
  open_regions: string[]
}

/** A LiteLLM proxy this app didn't deploy (e.g. on AKS). */
export type GatewayConnect = {
  tenant_id: string | null
  /** Searched for Foundry deployments. */
  subscription_id: string
  name: string
  url: string
  master_key: string
  principal_id?: string | null
  app_insights_id?: string | null
}

export type DeployStep = { resource_type: string; name: string; state: string; error: string | null }

export type GatewayPlatform = "aca" | "aks"

export type GatewayDeployStatus = {
  name: string
  state: "none" | "running" | "succeeded" | "failed"
  started_at: string | null
  steps: DeployStep[]
  error: string | null
  platform: GatewayPlatform
  /** azure: the ARM deployment; cluster: installing LiteLLM (AKS); certificate: waiting for HTTPS. */
  phase: "azure" | "cluster" | "certificate" | "done"
  /** The end of the install's output, once it has finished or failed. */
  log_tail: string | null
  /** Deployed with cleanup_on_failure: a failure of its Azure resources is cleaned up. */
  auto_cleanup: boolean
  cleanup: DeployCleanup | null
}

/** Deleting what a failed deployment created. */
export type DeployCleanup = {
  state: "running" | "done" | "failed"
  deleted: string[]
  /** Azure is still deleting these (an environment takes its time). */
  deleting: string[]
  error: string | null
}

export type AksBundle = { name: string; namespace: string; files: { path: string; content: string }[] }

// -- trying a pool (mirrors pool_manager/models.py) --

/** "pool": the key may call the pool (or one of its aliases); "all": every model. */
export type KeyScope = "pool" | "all"

/** A LiteLLM virtual key that can call the pool; only a hash of it is kept. */
export type VirtualKeyView = {
  id: string
  alias: string | null
  /** How LiteLLM abbreviates the key, e.g. sk-...Ab12. */
  key_name: string | null
  scope: KeyScope
  models: string[]
  blocked: boolean
  expires: string | null
  spend: number | null
  /** The test key this app creates for the pool. */
  managed: boolean
}

/** A key just created; the only time it can be shown. */
export type VirtualKeySecret = { key: string; view: VirtualKeyView }

export type ApiTestRequest = {
  path: string
  key: string | null
  body: Record<string, unknown>
}

export type BenchmarkStart = {
  spec: BenchmarkSpec
  key: string
}

// -- pool dashboard (mirrors pool_manager/models.py) --

export type CostItem = {
  kind: "container_apps" | "environment" | "postgres" | "redis" | "aks_nodes" | "aks_tier"
  detail: string
  monthly_low: number
  monthly_high: number
}

/** A retail price estimate, before discounts, free grants and tax. */
export type ListPrice = { items: CostItem[]; monthly_low: number; monthly_high: number; currency: string }

export type ServiceCost = {
  profile: string | null
  location: string | null
  list_price: ListPrice | null
  month_to_date: MonthToDate | null
  month_to_date_note: CostNote | null
  /** Azure's own message when month_to_date failed. */
  detail: string | null
  portal_url: string | null
}

// -- deleting a gateway (mirrors pool_manager/models.py) --

/** What deleting a gateway would remove, shown before the user confirms. */
export type GatewayDeleteCheck = {
  name: string
  resource_group: string
  location: string
  /** The gateway the app manages now. */
  current: boolean
  /** False when its deployment failed before the Container App existed. */
  deployed: boolean
  pools: number | null
  /** Resources tagged as the gateway's, deleted with it (never the resource group). */
  resources: string[]
}
