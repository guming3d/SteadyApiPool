import { keepPreviousData, queryOptions } from "@tanstack/react-query"

import type {
  AksBundle,
  AliasView,
  ApiTestRequest,
  ApiTestResult,
  BackendView,
  Capabilities,
  CleanupConfirm,
  ConfigPreview,
  ConfigView,
  DeployableModel,
  DeploymentImportResult,
  DeploymentRef,
  DeploymentView,
  DeviceCodePrompt,
  DeviceCodeStatus,
  FoundryAccountView,
  FoundryDeploymentCreate,
  FoundryDeploymentStatus,
  GatewayConnect,
  GatewayCreate,
  GatewayCreateOptions,
  GatewayDeleteCheck,
  GatewayDeployStatus,
  GatewayView,
  MemberBackendWrite,
  MetricsRange,
  ModelCapacity,
  ModelCheck,
  PoolEndpoint,
  PoolMetrics,
  PoolView,
  PoolWrite,
  Principal,
  RemovalConfirm,
  RemovalPlan,
  RemovalResult,
  ServiceCost,
  SetupCheck,
  SetupProgress,
  SetupReport,
  SubscriptionView,
  TargetSelect,
  TargetView,
  TenantView,
  VirtualKeySecret,
  VirtualKeyView,
} from "./types"

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
    this.name = "ApiError"
  }
}

type HeaderSource = () => Promise<Record<string, string>>
let extraHeaders: HeaderSource = async () => ({})

/** The public app adds the user's bearer token and their chosen gateway (src/session.ts). */
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
const scope = (tenantId: string | null, subscriptionId: string) =>
  new URLSearchParams({ subscription_id: subscriptionId, ...(tenantId ? { tenant_id: tenantId } : {}) }).toString()

export type AksBundleParams = {
  subscription_id: string
  resource_group: string
  cluster: string
  name: string
  location: string
  namespace?: string
}

export const api = {
  me: () => request<Principal>("/api/me"),
  capabilities: () => request<Capabilities>("/api/capabilities"),
  backends: () => request<BackendView[]>("/api/backends"),
  backend: (backendId: string) => request<BackendView>(`/api/backends/${id(backendId)}`),
  saveBackend: (backendId: string, body: MemberBackendWrite) =>
    request<BackendView>(`/api/backends/${id(backendId)}`, json("PUT", body)),
  // Only members no pool uses yet.
  deleteBackend: (backendId: string) =>
    request<void>(`/api/backends/${id(backendId)}`, { method: "DELETE" }),
  pools: () => request<PoolView[]>("/api/pools"),
  // Read-only: the same-model check saving a pool enforces.
  checkModels: (members: string[]) =>
    request<ModelCheck>("/api/pools/model-check", json("POST", { members })),
  pool: (poolId: string) => request<PoolView>(`/api/pools/${id(poolId)}`),
  savePool: (poolId: string, body: PoolWrite) =>
    request<PoolView>(`/api/pools/${id(poolId)}`, json("PUT", body)),
  // Read-only on the server: the LiteLLM config a save would write.
  previewPool: (poolId: string, body: PoolWrite) =>
    request<ConfigPreview>(`/api/pools/${id(poolId)}/preview`, json("POST", body)),
  poolConfig: (poolId: string) => request<ConfigView>(`/api/pools/${id(poolId)}/config`),
  // Read-only: what removing the pool takes with it (its aliases and test keys).
  poolRemoval: (poolId: string) => request<RemovalPlan>(`/api/pools/${id(poolId)}/removal`),
  removePool: (poolId: string, body: RemovalConfirm) =>
    request<RemovalResult>(`/api/pools/${id(poolId)}/remove`, json("POST", body)),
  // This month's spend (read with the user's own Azure rights) and a list price estimate.
  serviceCost: (refresh = false) => request<ServiceCost>(`/api/cost${refresh ? "?refresh=true" : ""}`),
  // Read-only: everything this app created on the gateway.
  cleanupPlan: () => request<RemovalPlan>("/api/cleanup"),
  cleanup: (body: CleanupConfirm) => request<RemovalResult>("/api/cleanup", json("POST", body)),
  // Read-only; the server caches them for a minute unless refresh is set.
  poolMetrics: (poolId: string, range: MetricsRange, refresh = false) =>
    request<PoolMetrics>(`/api/pools/${id(poolId)}/metrics?range=${range}${refresh ? "&refresh=true" : ""}`),
  endpoint: (poolId: string) => request<PoolEndpoint>(`/api/pools/${id(poolId)}/endpoint`),
  aliases: () => request<AliasView[]>("/api/aliases"),
  addAlias: (poolId: string, name: string) =>
    request<PoolEndpoint>(`/api/pools/${id(poolId)}/aliases`, json("POST", { name })),
  removeAlias: (poolId: string, name: string) =>
    request<PoolEndpoint>(`/api/pools/${id(poolId)}/aliases/${id(name)}`, { method: "DELETE" }),
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
  // Read-only: whether the gateway's identity can call these Foundry accounts.
  foundryAccess: (accountIds: string[]) =>
    request<SetupCheck[]>("/api/setup/foundry-access", json("POST", { account_ids: accountIds })),
  grantFoundryAccess: (accountIds: string[]) =>
    request<SetupCheck[]>("/api/setup/foundry-access/grant", json("POST", { account_ids: accountIds })),
  target: () => request<TargetView>("/api/target"),
  tenants: () => request<TenantView[]>("/api/target/tenants"),
  subscriptions: (tenantId: string | null) =>
    request<SubscriptionView[]>(`/api/target/subscriptions${tenantId ? `?tenant_id=${id(tenantId)}` : ""}`),
  gateways: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayView[]>(`/api/target/gateways?${scope(tenantId, subscriptionId)}`),
  createOptions: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayCreateOptions>(`/api/target/create-options?${scope(tenantId, subscriptionId)}`),
  selectTarget: (body: TargetSelect) => request<TargetView>("/api/target", json("PUT", body)),
  // Deploys billable Azure resources with the user's own rights, then selects the new gateway.
  createGateway: (body: GatewayCreate) => request<GatewayView>("/api/target/gateways", json("POST", body)),
  deployStatus: (tenantId: string | null, subscriptionId: string, resourceGroup: string, name: string) =>
    request<GatewayDeployStatus>(
      `/api/target/deploy-status?${scope(tenantId, subscriptionId)}&${new URLSearchParams({ resource_group: resourceGroup, name })}`,
    ),
  // Files that deploy the same gateway on an existing AKS cluster; nothing is written.
  aksBundle: (params: AksBundleParams) =>
    request<AksBundle>(`/api/target/aks-bundle?${new URLSearchParams(params)}`),
  // Checks the URL and master key against the gateway, then selects it.
  connectGateway: (body: GatewayConnect) => request<TargetView>("/api/target/connect", json("POST", body)),
  disconnectGateway: () => request<void>("/api/target/disconnect", { method: "POST" }),
  // An AKS gateway whose install in the cluster failed: run it again on the next status.
  retryInstall: (body: TargetSelect) => request<void>("/api/target/gateways/retry-install", json("POST", body)),
  // Only gateways this app deployed; their resources go, the resource group stays.
  deleteCheck: (body: TargetSelect) =>
    request<GatewayDeleteCheck>("/api/target/gateways/delete-check", json("POST", body)),
  deleteGateway: (body: TargetSelect & { confirm_name: string }) =>
    request<void>("/api/target/gateways/delete", json("POST", body)),
  // LiteLLM virtual keys; a key is only shown when it is created.
  poolKeys: (poolId: string) => request<VirtualKeyView[]>(`/api/pools/${id(poolId)}/keys`),
  createPoolKey: (poolId: string) =>
    request<VirtualKeySecret>(`/api/pools/${id(poolId)}/keys`, { method: "POST" }),
  // The backend sends the request, so the browser needs no CORS from the gateway.
  testPool: (poolId: string, body: ApiTestRequest) =>
    request<ApiTestResult>(`/api/pools/${id(poolId)}/test`, json("POST", body)),
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
  endpoint: (poolId: string) => ["pools", poolId, "endpoint"] as const,
  poolConfig: (poolId: string) => ["pools", poolId, "config"] as const,
  poolKeys: (poolId: string) => ["pools", poolId, "keys"] as const,
  aliases: ["aliases"] as const,
  deployments: ["deployments"] as const,
  foundryAccounts: ["foundry", "accounts"] as const,
  foundryModels: (accountId: string) => ["foundry", "models", accountId] as const,
  foundryCapacity: (accountId: string, model: string, version: string) => ["foundry", "capacity", accountId, model, version] as const,
  setup: ["setup"] as const,
  // Not under "setup": invalidating the report must not refetch this.
  setupProgress: ["setup-progress"] as const,
  foundryAccess: (accountIds: string[]) => ["setup", "foundry", ...accountIds] as const,
  target: ["target"] as const,
  tenants: ["target", "tenants"] as const,
  subscriptions: (tenantId: string | null) => ["target", "subscriptions", tenantId] as const,
  gateways: (tenantId: string | null, subscriptionId: string) => ["target", "gateways", tenantId, subscriptionId] as const,
  createOptions: (tenantId: string | null, subscriptionId: string) => ["target", "create-options", tenantId, subscriptionId] as const,
  deployStatus: (subscriptionId: string, resourceGroup: string, name: string) =>
    ["target", "deploy", subscriptionId, resourceGroup, name] as const,
}

export const queries = {
  me: () => queryOptions({ queryKey: keys.me, queryFn: api.me }),
  capabilities: () => queryOptions({ queryKey: keys.capabilities, queryFn: api.capabilities }),
  backends: () => queryOptions({ queryKey: keys.backends, queryFn: api.backends }),
  pools: () => queryOptions({ queryKey: keys.pools, queryFn: api.pools }),
  // The editor always reads the pool itself before saving, for its ETag.
  pool: (poolId: string) => queryOptions({ queryKey: keys.pool(poolId), queryFn: () => api.pool(poolId) }),
  endpoint: (poolId: string) => queryOptions({ queryKey: keys.endpoint(poolId), queryFn: () => api.endpoint(poolId) }),
  poolConfig: (poolId: string) => queryOptions({ queryKey: keys.poolConfig(poolId), queryFn: () => api.poolConfig(poolId) }),
  aliases: () => queryOptions({ queryKey: keys.aliases, queryFn: api.aliases }),
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
    // Spans and platform metrics land within minutes; changing the range keeps the old frame.
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
    // A new gateway takes a while to deploy; follow it without a manual refresh.
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
  poolKeys: (poolId: string, enabled: boolean) => queryOptions({
    queryKey: keys.poolKeys(poolId),
    queryFn: () => api.poolKeys(poolId),
    enabled,
    staleTime: 60_000,
    // Missing write access fails the same way every time; the guide explains.
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
  gateways: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.gateways(tenantId, subscriptionId),
    queryFn: () => api.gateways(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 60_000,
  }),
  // Followed every few seconds while a deployment runs.
  deployStatus: (tenantId: string | null, subscriptionId: string, resourceGroup: string, name: string) => queryOptions({
    queryKey: keys.deployStatus(subscriptionId, resourceGroup, name),
    queryFn: () => api.deployStatus(tenantId, subscriptionId, resourceGroup, name),
    enabled: Boolean(subscriptionId && resourceGroup && name),
    refetchInterval: (query) => query.state.data?.state === "running" ? 5_000 : false,
    retry: false,
  }),
  createOptions: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.createOptions(tenantId, subscriptionId),
    queryFn: () => api.createOptions(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 300_000,
  }),
}
