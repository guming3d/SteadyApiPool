// The API both gateways share: sign-in and setup, the chosen gateway, Foundry deployments.
// Each product's own calls are in apim/api.ts and litellm/api.ts, which include these.
import { keepPreviousData, queryOptions } from "@tanstack/react-query"

import type {
  DeployableModel,
  DeploymentImportResult,
  DeploymentRef,
  DeploymentView,
  DeviceCodePrompt,
  DeviceCodeStatus,
  FoundryAccountView,
  FoundryDeploymentCreate,
  FoundryDeploymentStatus,
  MetricsRange,
  ModelCapacity,
  PoolMetrics,
  Principal,
  Product,
  SetupCheck,
  SetupProgress,
  SetupReport,
  SubscriptionView,
  TargetView,
  TenantView,
} from "./types"

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message)
    this.name = "ApiError"
  }
}

type HeaderSource = () => Promise<Record<string, string>>
let extraHeaders: HeaderSource = async () => ({})

/** The public app adds the user's bearer token and their chosen service (src/session.ts). */
export function configureRequests(source: HeaderSource) {
  extraHeaders = source
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
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

export const id = encodeURIComponent
export const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })
export const scope = (tenantId: string | null, subscriptionId: string) =>
  new URLSearchParams({ subscription_id: subscriptionId, ...(tenantId ? { tenant_id: tenantId } : {}) }).toString()

export const api = {
  me: () => request<Principal>("/api/me"),
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
  // A pool's Monitor tab, which both products answer the same way under their own prefix.
  // The server caches it for a minute unless refresh is set.
  poolMetrics: (product: Product, poolId: string, range: MetricsRange, refresh = false) =>
    request<PoolMetrics>(`/api/${product}/pools/${id(poolId)}/metrics?range=${range}${refresh ? "&refresh=true" : ""}`),
  target: () => request<TargetView>("/api/target"),
  tenants: () => request<TenantView[]>("/api/target/tenants"),
  subscriptions: (tenantId: string | null) =>
    request<SubscriptionView[]>(`/api/target/subscriptions${tenantId ? `?tenant_id=${id(tenantId)}` : ""}`),
}

export const keys = {
  me: ["me"] as const,
  deployments: ["deployments"] as const,
  foundryAccounts: ["foundry", "accounts"] as const,
  foundryModels: (accountId: string) => ["foundry", "models", accountId] as const,
  foundryCapacity: (accountId: string, model: string, version: string) => ["foundry", "capacity", accountId, model, version] as const,
  setup: ["setup"] as const,
  // Not under "setup": invalidating the report must not refetch this.
  setupProgress: ["setup-progress"] as const,
  foundryAccess: (accountIds: string[]) => ["setup", "foundry", ...accountIds] as const,
  poolMetrics: (product: Product, poolId: string, range: MetricsRange) => [product, "pools", poolId, "metrics", range] as const,
  target: ["target"] as const,
  tenants: ["target", "tenants"] as const,
  subscriptions: (tenantId: string | null) => ["target", "subscriptions", tenantId] as const,
}

export const queries = {
  me: () => queryOptions({ queryKey: keys.me, queryFn: api.me }),
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
    // A new gateway (or an APIM identity) takes a while; follow it without a manual refresh.
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
  poolMetrics: (product: Product, poolId: string, range: MetricsRange, enabled: boolean) => queryOptions({
    queryKey: keys.poolMetrics(product, poolId, range),
    queryFn: () => api.poolMetrics(product, poolId, range),
    enabled,
    // Spans and platform metrics land within minutes; changing the range keeps the old frame.
    refetchInterval: 60_000,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
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
}
