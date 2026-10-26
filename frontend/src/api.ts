import { keepPreviousData, queryOptions } from "@tanstack/react-query"

import type {
  ApimCreate,
  ApimCreateOptions,
  ApimServiceView,
  ApiBindingWrite,
  ApiTestRequest,
  ApiTestResult,
  ApiView,
  BackendView,
  Capabilities,
  CleanupConfirm,
  DeletedServiceView,
  DeploymentImportResult,
  DeviceCodePrompt,
  DeviceCodeStatus,
  DeploymentRef,
  DeploymentView,
  MemberBackendWrite,
  MetricsRange,
  ModelCheck,
  PolicyPreview,
  PolicyView,
  PoolApiCreate,
  PoolMetrics,
  PoolView,
  PoolWrite,
  Principal,
  RemovalConfirm,
  RemovalPlan,
  RemovalResult,
  ServiceCost,
  ServiceDeleteCheck,
  SetupCheck,
  SetupProgress,
  SetupReport,
  SubscriptionKeyView,
  SubscriptionView,
  TargetSelect,
  TargetView,
  TenantView,
  DeployableModel,
  FoundryAccountView,
  FoundryDeploymentCreate,
  FoundryDeploymentStatus,
  ModelCapacity,
} from "./types"

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
    this.name = "ApiError"
  }
}

type HeaderSource = () => Promise<Record<string, string>>
let extraHeaders: HeaderSource = async () => ({})

/** The public app adds the user's bearer token and their chosen APIM service (src/session.ts). */
export function configureRequests(source: HeaderSource) {
  extraHeaders = source
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const write = init?.method && init.method !== "GET"
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      // Required by the backend for every write; see pool_manager/auth.py.
      ...(write ? { "X-Pool-Manager": "1" } : {}),
      ...await extraHeaders(),
      ...init?.headers,
    },
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json() as { detail?: unknown }
      if (typeof body.detail === "string") detail = body.detail
      else if (Array.isArray(body.detail)) {
        detail = body.detail
          .map((item: { loc?: unknown[]; msg?: string }) => `${(item.loc ?? []).slice(1).join(".")}: ${item.msg}`)
          .join("; ")
      }
    } catch {}
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const id = encodeURIComponent
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })
const withEtag = (path: string, etag?: string | null) =>
  etag ? `${path}?etag=${encodeURIComponent(etag)}` : path
const scope = (tenantId: string | null, subscriptionId: string) =>
  new URLSearchParams({ subscription_id: subscriptionId, ...(tenantId ? { tenant_id: tenantId } : {}) }).toString()

export const api = {
  me: () => request<Principal>("/api/me"),
  capabilities: () => request<Capabilities>("/api/capabilities"),
  backends: () => request<BackendView[]>("/api/backends"),
  backend: (backendId: string) => request<BackendView>(`/api/backends/${id(backendId)}`),
  saveBackend: (backendId: string, body: MemberBackendWrite) =>
    request<BackendView>(`/api/backends/${id(backendId)}`, json("PUT", body)),
  deleteBackend: (backendId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/backends/${id(backendId)}`, etag), { method: "DELETE" }),
  pools: () => request<PoolView[]>("/api/pools"),
  // Read-only: the same-model check saving a pool enforces.
  checkModels: (members: string[]) =>
    request<ModelCheck>("/api/pools/model-check", json("POST", { members })),
  pool: (poolId: string) => request<PoolView>(`/api/pools/${id(poolId)}`),
  savePool: (poolId: string, body: PoolWrite) =>
    request<PoolView>(`/api/pools/${id(poolId)}`, json("PUT", body)),
  deletePool: (poolId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/pools/${id(poolId)}`, etag), { method: "DELETE" }),
  // Read-only: what removing the pool takes with it (its APIs and unused members).
  poolRemoval: (poolId: string) => request<RemovalPlan>(`/api/pools/${id(poolId)}/removal`),
  removePool: (poolId: string, body: RemovalConfirm) =>
    request<RemovalResult>(`/api/pools/${id(poolId)}/remove`, json("POST", body)),
  // This month's spend (read with the user's own Azure rights) and the tier's list price.
  serviceCost: (refresh = false) => request<ServiceCost>(`/api/cost${refresh ? "?refresh=true" : ""}`),
  // Read-only: everything this app created in the service.
  cleanupPlan: () => request<RemovalPlan>("/api/cleanup"),
  cleanup: (body: CleanupConfirm) => request<RemovalResult>("/api/cleanup", json("POST", body)),
  // Read-only Azure Monitor metrics; the server caches them for a minute unless refresh is set.
  poolMetrics: (poolId: string, range: MetricsRange, refresh = false) =>
    request<PoolMetrics>(`/api/pools/${id(poolId)}/metrics?range=${range}${refresh ? "&refresh=true" : ""}`),
  apis: () => request<ApiView[]>("/api/apis"),
  createPoolApi: (poolId: string, body: PoolApiCreate) =>
    request<ApiView>(`/api/pools/${id(poolId)}/api`, json("POST", body)),
  deployments: (refresh = false) =>
    request<DeploymentView[]>(`/api/deployments${refresh ? "?refresh=true" : ""}`),
  importDeployments: (deployments: DeploymentRef[]) =>
    request<DeploymentImportResult>("/api/deployments/import", json("POST", { deployments })),
  // Deploying models runs with the user's own Azure rights (see model_deploy.py).
  foundryAccounts: () => request<FoundryAccountView[]>("/api/foundry/accounts"),
  foundryModels: (accountId: string) =>
    request<DeployableModel[]>(`/api/foundry/models?${new URLSearchParams({ account_id: accountId })}`),
  foundryCapacity: (accountId: string, model: string, version: string) =>
    request<ModelCapacity>(`/api/foundry/capacity?${new URLSearchParams({ account_id: accountId, model, version })}`),
  createFoundryDeployment: (body: FoundryDeploymentCreate) =>
    request<FoundryDeploymentStatus>("/api/foundry/deployments", json("POST", body)),
  foundryDeploymentStatus: (accountId: string, deployment: string) =>
    request<FoundryDeploymentStatus>(`/api/foundry/deployments/status?${new URLSearchParams({ account_id: accountId, deployment })}`),
  policy: (apiId: string) => request<PolicyView>(`/api/apis/${id(apiId)}/policy`),
  // Read-only on the server; returns the policy a bind would write.
  previewBinding: (apiId: string, body: ApiBindingWrite) =>
    request<PolicyPreview>(`/api/apis/${id(apiId)}/binding/preview`, json("POST", body)),
  bind: (apiId: string, body: ApiBindingWrite) =>
    request<ApiView>(`/api/apis/${id(apiId)}/binding`, json("PUT", body)),
  unbind: (apiId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/apis/${id(apiId)}/binding`, etag), { method: "DELETE" }),
  setup: (refresh = false) => request<SetupReport>(`/api/setup${refresh ? "?refresh=true" : ""}`),
  // Which step the setup report above is on while it runs.
  setupProgress: () => request<SetupProgress>("/api/setup/progress"),
  // Local only: opens the browser sign-in on this machine and resolves once it completes.
  signIn: (tenantId?: string) =>
    request<void>("/api/setup/sign-in", tenantId ? json("POST", { tenant_id: tenantId }) : { method: "POST" }),
  // Local only: a sign-in for machines without a local browser; GET reports when it finished.
  startDeviceCode: (tenantId?: string) =>
    request<DeviceCodePrompt>("/api/setup/sign-in/device-code", tenantId ? json("POST", { tenant_id: tenantId }) : { method: "POST" }),
  deviceCodeStatus: () => request<DeviceCodeStatus>("/api/setup/sign-in/device-code"),
  // Local only: forgets the app's own sign-in (not az / azd / PowerShell).
  signOut: () => request<void>("/api/setup/sign-out", { method: "POST" }),
  enableApimIdentity: () => request<SetupCheck>("/api/setup/apim-identity", { method: "POST" }),
  // Read-only: whether the APIM identity can call these Foundry accounts.
  foundryAccess: (accountIds: string[]) =>
    request<SetupCheck[]>("/api/setup/foundry-access", json("POST", { account_ids: accountIds })),
  grantFoundryAccess: (accountIds: string[]) =>
    request<SetupCheck[]>("/api/setup/foundry-access/grant", json("POST", { account_ids: accountIds })),
  target: () => request<TargetView>("/api/target"),
  tenants: () => request<TenantView[]>("/api/target/tenants"),
  subscriptions: (tenantId: string | null) =>
    request<SubscriptionView[]>(`/api/target/subscriptions${tenantId ? `?tenant_id=${id(tenantId)}` : ""}`),
  services: (tenantId: string | null, subscriptionId: string) =>
    request<ApimServiceView[]>(`/api/target/services?${scope(tenantId, subscriptionId)}`),
  createOptions: (tenantId: string | null, subscriptionId: string) =>
    request<ApimCreateOptions>(`/api/target/create-options?${scope(tenantId, subscriptionId)}`),
  selectTarget: (body: TargetSelect) => request<TargetView>("/api/target", json("PUT", body)),
  // Creates billable Azure resources with the user's own rights, then selects the new service.
  createService: (body: ApimCreate) => request<ApimServiceView>("/api/target/services", json("POST", body)),
  // Only services this app created; Azure soft-deletes them and keeps them 48 hours.
  deleteCheck: (body: TargetSelect) =>
    request<ServiceDeleteCheck>("/api/target/services/delete-check", json("POST", body)),
  deleteService: (body: TargetSelect & { confirm_name: string }) =>
    request<void>("/api/target/services/delete", json("POST", body)),
  deletedServices: (tenantId: string | null, subscriptionId: string) =>
    request<DeletedServiceView[]>(`/api/target/deleted?${scope(tenantId, subscriptionId)}`),
  restoreService: (body: { tenant_id: string | null; subscription_id: string; location: string; name: string }) =>
    request<ApimServiceView>("/api/target/deleted/restore", json("POST", body)),
  // Subscription keys are read with the user's own Azure rights.
  apiKeys: (apiId: string) => request<SubscriptionKeyView[]>(`/api/apis/${id(apiId)}/keys`),
  createApiKey: (apiId: string) => request<SubscriptionKeyView>(`/api/apis/${id(apiId)}/keys`, { method: "POST" }),
  readApiKey: (apiId: string, keyId: string) =>
    request<{ primary_key: string }>(`/api/apis/${id(apiId)}/keys/${id(keyId)}/secret`, { method: "POST" }),
  // The backend sends the request: browsers can't call the APIM gateway (no CORS).
  testApi: (apiId: string, body: ApiTestRequest) =>
    request<ApiTestResult>(`/api/apis/${id(apiId)}/test`, json("POST", body)),
}

export const keys = {
  me: ["me"] as const,
  capabilities: ["capabilities"] as const,
  backends: ["backends"] as const,
  pools: ["pools"] as const,
  pool: (poolId: string) => ["pools", poolId] as const,
  // Outside ["pools"], so refreshing the pool list never re-reads a plan nobody is looking at.
  poolRemoval: (poolId: string) => ["removal", "pool", poolId] as const,
  cleanup: ["removal", "cleanup"] as const,
  cost: ["cost"] as const,
  modelCheck: (members: string[]) => ["model-check", ...members] as const,
  poolMetrics: (poolId: string, range: MetricsRange) => ["pools", poolId, "metrics", range] as const,
  apis: ["apis"] as const,
  policy: (apiId: string) => ["apis", apiId, "policy"] as const,
  deployments: ["deployments"] as const,
  foundryAccounts: ["foundry", "accounts"] as const,
  foundryModels: (accountId: string) => ["foundry", "models", accountId] as const,
  foundryCapacity: (accountId: string, model: string, version: string) => ["foundry", "capacity", accountId, model, version] as const,
  setup: ["setup"] as const,
  // Not under "setup": invalidating the report must not refetch this.
  setupProgress: ["setup-progress"] as const,
  foundryAccess: (accountIds: string[]) => ["setup", "foundry", ...accountIds] as const,
  apiKeys: (apiId: string) => ["apis", apiId, "keys"] as const,
  target: ["target"] as const,
  tenants: ["target", "tenants"] as const,
  subscriptions: (tenantId: string | null) => ["target", "subscriptions", tenantId] as const,
  services: (tenantId: string | null, subscriptionId: string) => ["target", "services", tenantId, subscriptionId] as const,
  createOptions: (tenantId: string | null, subscriptionId: string) => ["target", "create-options", tenantId, subscriptionId] as const,
  deleted: (tenantId: string | null, subscriptionId: string) => ["target", "deleted", tenantId, subscriptionId] as const,
}

export const queries = {
  me: () => queryOptions({ queryKey: keys.me, queryFn: api.me }),
  capabilities: () => queryOptions({ queryKey: keys.capabilities, queryFn: api.capabilities }),
  backends: () => queryOptions({ queryKey: keys.backends, queryFn: api.backends }),
  pools: () => queryOptions({ queryKey: keys.pools, queryFn: api.pools }),
  // List responses carry no ETag; the editor always reads the pool itself before saving.
  pool: (poolId: string) => queryOptions({ queryKey: keys.pool(poolId), queryFn: () => api.pool(poolId) }),
  apis: () => queryOptions({ queryKey: keys.apis, queryFn: api.apis }),
  // Read fresh each time a removal dialog opens: it must show what is there now.
  poolRemoval: (poolId: string) => queryOptions({
    queryKey: keys.poolRemoval(poolId),
    queryFn: () => api.poolRemoval(poolId),
    staleTime: 0,
    gcTime: 0,
    retry: false,
  }),
  // The server caches spend for an hour and prices for a day; Refresh bypasses that.
  cost: () => queryOptions({ queryKey: keys.cost, queryFn: () => api.serviceCost(), staleTime: 300_000, retry: false }),
  cleanup: () => queryOptions({
    queryKey: keys.cleanup,
    queryFn: api.cleanupPlan,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  }),
  poolMetrics: (poolId: string, range: MetricsRange, enabled: boolean) => queryOptions({
    queryKey: keys.poolMetrics(poolId, range),
    queryFn: () => api.poolMetrics(poolId, range),
    enabled,
    // Platform metrics land every minute; changing the range keeps the old frame until it loads.
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  }),
  modelCheck: (members: string[]) => queryOptions({
    queryKey: keys.modelCheck(members),
    queryFn: () => api.checkModels(members),
    enabled: members.length > 0,
    staleTime: 60_000,
  }),
  policy: (apiId: string) => queryOptions({ queryKey: keys.policy(apiId), queryFn: () => api.policy(apiId) }),
  // The server caches scans for 5 minutes; Refresh bypasses that.
  deployments: () => queryOptions({ queryKey: keys.deployments, queryFn: () => api.deployments(), staleTime: 60_000 }),
  foundryAccounts: () => queryOptions({ queryKey: keys.foundryAccounts, queryFn: api.foundryAccounts, staleTime: 300_000 }),
  foundryModels: (accountId: string) => queryOptions({
    queryKey: keys.foundryModels(accountId),
    queryFn: () => api.foundryModels(accountId),
    enabled: accountId !== "",
    staleTime: 300_000,
  }),
  // Quota moves as people deploy; read it fresh for each choice.
  foundryCapacity: (accountId: string, model: string, version: string) => queryOptions({
    queryKey: keys.foundryCapacity(accountId, model, version),
    queryFn: () => api.foundryCapacity(accountId, model, version),
    enabled: accountId !== "" && model !== "" && version !== "",
    staleTime: 30_000,
  }),
  setup: () => queryOptions({
    queryKey: keys.setup,
    queryFn: () => api.setup(),
    // A new APIM service, or its identity, takes a while; follow it without a manual refresh.
    refetchInterval: (query) => query.state.data?.checks.some((check) => check.status === "pending") ? 5_000 : false,
  }),
  // Polled only while something waits for the report.
  setupProgress: () => queryOptions({
    queryKey: keys.setupProgress,
    queryFn: api.setupProgress,
    refetchInterval: 700,
    gcTime: 0,
    retry: false,
  }),
  foundryAccess: (accountIds: string[]) => queryOptions({
    queryKey: keys.foundryAccess(accountIds),
    queryFn: () => api.foundryAccess(accountIds),
    enabled: accountIds.length > 0,
    staleTime: 60_000,
  }),
  apiKeys: (apiId: string) => queryOptions({
    queryKey: keys.apiKeys(apiId),
    queryFn: () => api.apiKeys(apiId),
    staleTime: 60_000,
    // Missing rights or no delegated token fail the same way every time; the guide explains.
    retry: false,
  }),
  target: () => queryOptions({ queryKey: keys.target, queryFn: api.target }),
  tenants: () => queryOptions({ queryKey: keys.tenants, queryFn: api.tenants, staleTime: 300_000 }),
  subscriptions: (tenantId: string | null) => queryOptions({
    queryKey: keys.subscriptions(tenantId),
    queryFn: () => api.subscriptions(tenantId),
    staleTime: 300_000,
    // A tenant that needs another sign-in fails the same way on every retry.
    retry: false,
  }),
  services: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.services(tenantId, subscriptionId),
    queryFn: () => api.services(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 60_000,
  }),
  deleted: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.deleted(tenantId, subscriptionId),
    queryFn: () => api.deletedServices(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 30_000,
  }),
  createOptions: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.createOptions(tenantId, subscriptionId),
    queryFn: () => api.createOptions(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 300_000,
  }),
}
