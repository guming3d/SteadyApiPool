// AI Gateway's API, under /api/aigateway, with the shared calls (../api.ts) included.
import { queryOptions } from "@tanstack/react-query"

import { api as sharedApi, id, json, keys as sharedKeys, queries as sharedQueries, request, scope } from "../api"
import type { ApiTestResult, BenchmarkRun, ModelCheck, SetupCheck, TargetView } from "../types"
import type { ApiKeySecret, ApiKeyView, ApiTestRequest, BackendView, BenchmarkStart, Capabilities, CleanupConfirm, GatewayCreate, GatewayCreateOptions, GatewayDeleteCheck, GatewayView, MemberBackendWrite, PoolEndpoint, PoolView, PoolWrite, RemovalConfirm, RemovalPlan, RemovalResult, ServiceCost, TargetSelect } from "./types"

const withEtag = (path: string, etag?: string | null) =>
  etag ? `${path}?etag=${encodeURIComponent(etag)}` : path

export const api = {
  ...sharedApi,
  capabilities: () => request<Capabilities>("/api/aigateway/capabilities"),
  backends: () => request<BackendView[]>("/api/aigateway/backends"),
  backend: (backendId: string) => request<BackendView>(`/api/aigateway/backends/${id(backendId)}`),
  // Custom endpoints only; Foundry deployments are imported.
  saveBackend: (backendId: string, body: MemberBackendWrite) =>
    request<BackendView>(`/api/aigateway/backends/${id(backendId)}`, json("PUT", body)),
  // Only members no pool uses.
  deleteBackend: (backendId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/aigateway/backends/${id(backendId)}`, etag), { method: "DELETE" }),
  pools: () => request<PoolView[]>("/api/aigateway/pools"),
  // Read-only: the same-model check saving a pool enforces.
  checkModels: (members: string[]) =>
    request<ModelCheck>("/api/aigateway/pools/model-check", json("POST", { members })),
  pool: (poolId: string) => request<PoolView>(`/api/aigateway/pools/${id(poolId)}`),
  savePool: (poolId: string, body: PoolWrite) =>
    request<PoolView>(`/api/aigateway/pools/${id(poolId)}`, json("PUT", body)),
  endpoint: (poolId: string) => request<PoolEndpoint>(`/api/aigateway/pools/${id(poolId)}/endpoint`),
  // Read-only: what removing the pool takes with it (its unused members).
  poolRemoval: (poolId: string) => request<RemovalPlan>(`/api/aigateway/pools/${id(poolId)}/removal`),
  removePool: (poolId: string, body: RemovalConfirm) =>
    request<RemovalResult>(`/api/aigateway/pools/${id(poolId)}/remove`, json("POST", body)),
  // This month's spend (read with the user's own Azure rights) and the request price.
  serviceCost: (refresh = false) => request<ServiceCost>(`/api/aigateway/cost${refresh ? "?refresh=true" : ""}`),
  // Read-only: everything this app created on the gateway.
  cleanupPlan: () => request<RemovalPlan>("/api/aigateway/cleanup"),
  cleanup: (body: CleanupConfirm) => request<RemovalResult>("/api/aigateway/cleanup", json("POST", body)),
  // Turns on the gateway's system-assigned identity with the user's own rights.
  enableIdentity: () => request<SetupCheck>("/api/aigateway/setup/identity", { method: "POST" }),
  // Runtime keys, listed, created and read with the user's own Azure rights; each opens every pool.
  apiKeys: () => request<ApiKeyView[]>("/api/aigateway/keys"),
  createTestKey: () => request<ApiKeyView>("/api/aigateway/keys", { method: "POST" }),
  // POST, so the value is never cached or prefetched.
  keySecret: (keyId: string) => request<ApiKeySecret>(`/api/aigateway/keys/${id(keyId)}/secret`, { method: "POST" }),
  // The backend sends the request, so the browser needs no CORS from the gateway.
  testPool: (poolId: string, body: ApiTestRequest) =>
    request<ApiTestResult>(`/api/aigateway/pools/${id(poolId)}/test`, json("POST", body)),
  startBenchmark: (poolId: string, body: BenchmarkStart) =>
    request<BenchmarkRun>(`/api/aigateway/pools/${id(poolId)}/benchmarks`, json("POST", body)),
  benchmark: (poolId: string, runId: string) =>
    request<BenchmarkRun>(`/api/aigateway/pools/${id(poolId)}/benchmarks/${id(runId)}`),
  stopBenchmark: (poolId: string, runId: string) =>
    request<BenchmarkRun>(`/api/aigateway/pools/${id(poolId)}/benchmarks/${id(runId)}/stop`, { method: "POST" }),
  gateways: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayView[]>(`/api/aigateway/target/gateways?${scope(tenantId, subscriptionId)}`),
  createOptions: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayCreateOptions>(`/api/aigateway/target/create-options?${scope(tenantId, subscriptionId)}`),
  selectTarget: (body: TargetSelect) => request<TargetView>("/api/aigateway/target", json("PUT", body)),
  // Creates a billable gateway with the user's own rights, then selects it.
  createGateway: (body: GatewayCreate) => request<GatewayView>("/api/aigateway/target/gateways", json("POST", body)),
  // Only gateways this app created; the resource group stays.
  deleteCheck: (body: TargetSelect) =>
    request<GatewayDeleteCheck>("/api/aigateway/target/gateways/delete-check", json("POST", body)),
  deleteGateway: (body: TargetSelect & { confirm_name: string }) =>
    request<void>("/api/aigateway/target/gateways/delete", json("POST", body)),
}

export const keys = {
  ...sharedKeys,
  capabilities: ["aigateway", "capabilities"] as const,
  backends: ["aigateway", "backends"] as const,
  pools: ["aigateway", "pools"] as const,
  pool: (poolId: string) => ["aigateway", "pools", poolId] as const,
  // Outside ["pools"], so refreshing the pool list never re-reads a plan nobody is looking at.
  poolRemoval: (poolId: string) => ["aigateway", "removal", "pool", poolId] as const,
  cleanup: ["aigateway", "removal", "cleanup"] as const,
  cost: ["aigateway", "cost"] as const,
  modelCheck: (members: string[]) => ["aigateway", "model-check", ...members] as const,
  endpoint: (poolId: string) => ["aigateway", "pools", poolId, "endpoint"] as const,
  apiKeys: ["aigateway", "keys"] as const,
  benchmark: (poolId: string, runId: string) => ["aigateway", "benchmark", poolId, runId] as const,
  gateways: (tenantId: string | null, subscriptionId: string) => ["aigateway", "target", "gateways", tenantId, subscriptionId] as const,
  createOptions: (tenantId: string | null, subscriptionId: string) => ["aigateway", "target", "create-options", tenantId, subscriptionId] as const,
}

export const queries = {
  ...sharedQueries,
  capabilities: () => queryOptions({ queryKey: keys.capabilities, queryFn: api.capabilities }),
  backends: () => queryOptions({ queryKey: keys.backends, queryFn: api.backends }),
  pools: () => queryOptions({ queryKey: keys.pools, queryFn: api.pools }),
  // The editor always reads the pool itself before saving, for its ETag.
  pool: (poolId: string) => queryOptions({ queryKey: keys.pool(poolId), queryFn: () => api.pool(poolId) }),
  endpoint: (poolId: string) => queryOptions({ queryKey: keys.endpoint(poolId), queryFn: () => api.endpoint(poolId) }),
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
  modelCheck: (members: string[]) => queryOptions({
    queryKey: keys.modelCheck(members),
    queryFn: () => api.checkModels(members),
    enabled: members.length > 0,
    staleTime: 60_000,
  }),
  apiKeys: (enabled: boolean) => queryOptions({
    queryKey: keys.apiKeys,
    queryFn: api.apiKeys,
    enabled,
    staleTime: 60_000,
    // Missing rights fail the same way every time; the guide explains.
    retry: false,
  }),
  gateways: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.gateways(tenantId, subscriptionId),
    queryFn: () => api.gateways(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 60_000,
  }),
  createOptions: (tenantId: string | null, subscriptionId: string) => queryOptions({
    queryKey: keys.createOptions(tenantId, subscriptionId),
    queryFn: () => api.createOptions(tenantId, subscriptionId),
    enabled: Boolean(subscriptionId),
    staleTime: 300_000,
  }),
}
