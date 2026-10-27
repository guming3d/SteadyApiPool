// LiteLLM's API, under /api/litellm, with the shared calls (../api.ts) included.
import { queryOptions } from "@tanstack/react-query"

import { api as sharedApi, id, json, keys as sharedKeys, queries as sharedQueries, request, scope } from "../api"
import type { ApiTestResult, ModelCheck, TargetView } from "../types"
import type { AksBundle, AliasView, ApiTestRequest, BackendView, BenchmarkRun, BenchmarkStart, Capabilities, CleanupConfirm, ConfigPreview, ConfigView, GatewayConnect, GatewayCreate, GatewayCreateOptions, GatewayDeleteCheck, GatewayDeployStatus, GatewayView, MemberBackendWrite, PoolEndpoint, PoolView, PoolWrite, RemovalConfirm, RemovalPlan, RemovalResult, ServiceCost, TargetSelect, VirtualKeySecret, VirtualKeyView } from "./types"

export type AksBundleParams = {
  subscription_id: string
  resource_group: string
  cluster: string
  name: string
  location: string
  namespace?: string
}

export const api = {
  ...sharedApi,
  capabilities: () => request<Capabilities>("/api/litellm/capabilities"),
  backends: () => request<BackendView[]>("/api/litellm/backends"),
  backend: (backendId: string) => request<BackendView>(`/api/litellm/backends/${id(backendId)}`),
  saveBackend: (backendId: string, body: MemberBackendWrite) =>
    request<BackendView>(`/api/litellm/backends/${id(backendId)}`, json("PUT", body)),
  // Only members no pool uses yet.
  deleteBackend: (backendId: string) =>
    request<void>(`/api/litellm/backends/${id(backendId)}`, { method: "DELETE" }),
  pools: () => request<PoolView[]>("/api/litellm/pools"),
  // Read-only: the same-model check saving a pool enforces.
  checkModels: (members: string[]) =>
    request<ModelCheck>("/api/litellm/pools/model-check", json("POST", { members })),
  pool: (poolId: string) => request<PoolView>(`/api/litellm/pools/${id(poolId)}`),
  savePool: (poolId: string, body: PoolWrite) =>
    request<PoolView>(`/api/litellm/pools/${id(poolId)}`, json("PUT", body)),
  // Read-only on the server: the LiteLLM config a save would write.
  previewPool: (poolId: string, body: PoolWrite) =>
    request<ConfigPreview>(`/api/litellm/pools/${id(poolId)}/preview`, json("POST", body)),
  poolConfig: (poolId: string) => request<ConfigView>(`/api/litellm/pools/${id(poolId)}/config`),
  // Read-only: what removing the pool takes with it (its aliases and test keys).
  poolRemoval: (poolId: string) => request<RemovalPlan>(`/api/litellm/pools/${id(poolId)}/removal`),
  removePool: (poolId: string, body: RemovalConfirm) =>
    request<RemovalResult>(`/api/litellm/pools/${id(poolId)}/remove`, json("POST", body)),
  // This month's spend (read with the user's own Azure rights) and a list price estimate.
  serviceCost: (refresh = false) => request<ServiceCost>(`/api/litellm/cost${refresh ? "?refresh=true" : ""}`),
  // Read-only: everything this app created on the gateway.
  cleanupPlan: () => request<RemovalPlan>("/api/litellm/cleanup"),
  cleanup: (body: CleanupConfirm) => request<RemovalResult>("/api/litellm/cleanup", json("POST", body)),
  endpoint: (poolId: string) => request<PoolEndpoint>(`/api/litellm/pools/${id(poolId)}/endpoint`),
  aliases: () => request<AliasView[]>("/api/litellm/aliases"),
  addAlias: (poolId: string, name: string) =>
    request<PoolEndpoint>(`/api/litellm/pools/${id(poolId)}/aliases`, json("POST", { name })),
  removeAlias: (poolId: string, name: string) =>
    request<PoolEndpoint>(`/api/litellm/pools/${id(poolId)}/aliases/${id(name)}`, { method: "DELETE" }),
  gateways: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayView[]>(`/api/litellm/target/gateways?${scope(tenantId, subscriptionId)}`),
  createOptions: (tenantId: string | null, subscriptionId: string) =>
    request<GatewayCreateOptions>(`/api/litellm/target/create-options?${scope(tenantId, subscriptionId)}`),
  selectTarget: (body: TargetSelect) => request<TargetView>("/api/litellm/target", json("PUT", body)),
  // Deploys billable Azure resources with the user's own rights, then selects the new gateway.
  createGateway: (body: GatewayCreate) => request<GatewayView>("/api/litellm/target/gateways", json("POST", body)),
  deployStatus: (tenantId: string | null, subscriptionId: string, resourceGroup: string, name: string) =>
    request<GatewayDeployStatus>(
      `/api/litellm/target/deploy-status?${scope(tenantId, subscriptionId)}&${new URLSearchParams({ resource_group: resourceGroup, name })}`,
    ),
  // Files that deploy the same gateway on an existing AKS cluster; nothing is written.
  aksBundle: (params: AksBundleParams) =>
    request<AksBundle>(`/api/litellm/target/aks-bundle?${new URLSearchParams(params)}`),
  // Checks the URL and master key against the gateway, then selects it.
  connectGateway: (body: GatewayConnect) => request<TargetView>("/api/litellm/target/connect", json("POST", body)),
  disconnectGateway: () => request<void>("/api/litellm/target/disconnect", { method: "POST" }),
  // An AKS gateway whose install in the cluster failed: run it again on the next status.
  retryInstall: (body: TargetSelect) => request<void>("/api/litellm/target/gateways/retry-install", json("POST", body)),
  // Only gateways this app deployed; their resources go, the resource group stays.
  deleteCheck: (body: TargetSelect) =>
    request<GatewayDeleteCheck>("/api/litellm/target/gateways/delete-check", json("POST", body)),
  deleteGateway: (body: TargetSelect & { confirm_name: string }) =>
    request<void>("/api/litellm/target/gateways/delete", json("POST", body)),
  // LiteLLM virtual keys; a key is only shown when it is created.
  poolKeys: (poolId: string) => request<VirtualKeyView[]>(`/api/litellm/pools/${id(poolId)}/keys`),
  createPoolKey: (poolId: string) =>
    request<VirtualKeySecret>(`/api/litellm/pools/${id(poolId)}/keys`, { method: "POST" }),
  // The backend sends the request, so the browser needs no CORS from the gateway.
  testPool: (poolId: string, body: ApiTestRequest) =>
    request<ApiTestResult>(`/api/litellm/pools/${id(poolId)}/test`, json("POST", body)),
  startBenchmark: (poolId: string, body: BenchmarkStart) =>
    request<BenchmarkRun>(`/api/litellm/pools/${id(poolId)}/benchmarks`, json("POST", body)),
  benchmark: (poolId: string, runId: string) =>
    request<BenchmarkRun>(`/api/litellm/pools/${id(poolId)}/benchmarks/${id(runId)}`),
  stopBenchmark: (poolId: string, runId: string) =>
    request<BenchmarkRun>(`/api/litellm/pools/${id(poolId)}/benchmarks/${id(runId)}/stop`, { method: "POST" }),
}

export const keys = {
  ...sharedKeys,
  capabilities: ["litellm", "capabilities"] as const,
  backends: ["litellm", "backends"] as const,
  pools: ["litellm", "pools"] as const,
  pool: (poolId: string) => ["litellm", "pools", poolId] as const,
  // Outside ["pools"], so refreshing the pool list never re-reads a plan nobody is looking at.
  poolRemoval: (poolId: string) => ["litellm", "removal", "pool", poolId] as const,
  cleanup: ["litellm", "removal", "cleanup"] as const,
  cost: ["litellm", "cost"] as const,
  modelCheck: (members: string[]) => ["litellm", "model-check", ...members] as const,
  endpoint: (poolId: string) => ["litellm", "pools", poolId, "endpoint"] as const,
  poolConfig: (poolId: string) => ["litellm", "pools", poolId, "config"] as const,
  poolKeys: (poolId: string) => ["litellm", "pools", poolId, "keys"] as const,
  benchmark: (poolId: string, runId: string) => ["litellm", "benchmark", poolId, runId] as const,
  aliases: ["litellm", "aliases"] as const,
  gateways: (tenantId: string | null, subscriptionId: string) => ["litellm", "target", "gateways", tenantId, subscriptionId] as const,
  createOptions: (tenantId: string | null, subscriptionId: string) => ["litellm", "target", "create-options", tenantId, subscriptionId] as const,
  deployStatus: (subscriptionId: string, resourceGroup: string, name: string) =>
    ["litellm", "target", "deploy", subscriptionId, resourceGroup, name] as const,
}

export const queries = {
  ...sharedQueries,
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
  modelCheck: (members: string[]) => queryOptions({
    queryKey: keys.modelCheck(members),
    queryFn: () => api.checkModels(members),
    enabled: members.length > 0,
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
