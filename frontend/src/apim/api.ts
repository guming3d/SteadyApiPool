// APIM's API, under /api/apim, with the shared calls (../api.ts) included.
import { queryOptions } from "@tanstack/react-query"

import { api as sharedApi, id, json, keys as sharedKeys, queries as sharedQueries, request, scope } from "../api"
import type { ApiTestResult, ModelCheck, SetupCheck, TargetView } from "../types"
import type { ApiBindingWrite, ApiTestRequest, ApiView, ApimCreate, ApimCreateOptions, ApimServiceView, BackendView, Capabilities, CleanupConfirm, DeletedServiceView, MemberBackendWrite, PolicyPreview, PolicyView, PoolApiCreate, PoolView, PoolWrite, RemovalConfirm, RemovalPlan, RemovalResult, ServiceCost, ServiceDeleteCheck, SubscriptionKeyView, TargetSelect } from "./types"

const withEtag = (path: string, etag?: string | null) =>
  etag ? `${path}?etag=${encodeURIComponent(etag)}` : path

export const api = {
  ...sharedApi,
  capabilities: () => request<Capabilities>("/api/apim/capabilities"),
  backends: () => request<BackendView[]>("/api/apim/backends"),
  backend: (backendId: string) => request<BackendView>(`/api/apim/backends/${id(backendId)}`),
  saveBackend: (backendId: string, body: MemberBackendWrite) =>
    request<BackendView>(`/api/apim/backends/${id(backendId)}`, json("PUT", body)),
  deleteBackend: (backendId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/apim/backends/${id(backendId)}`, etag), { method: "DELETE" }),
  pools: () => request<PoolView[]>("/api/apim/pools"),
  // Read-only: the same-model check saving a pool enforces.
  checkModels: (members: string[]) =>
    request<ModelCheck>("/api/apim/pools/model-check", json("POST", { members })),
  pool: (poolId: string) => request<PoolView>(`/api/apim/pools/${id(poolId)}`),
  savePool: (poolId: string, body: PoolWrite) =>
    request<PoolView>(`/api/apim/pools/${id(poolId)}`, json("PUT", body)),
  deletePool: (poolId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/apim/pools/${id(poolId)}`, etag), { method: "DELETE" }),
  // Read-only: what removing the pool takes with it (its APIs and unused members).
  poolRemoval: (poolId: string) => request<RemovalPlan>(`/api/apim/pools/${id(poolId)}/removal`),
  removePool: (poolId: string, body: RemovalConfirm) =>
    request<RemovalResult>(`/api/apim/pools/${id(poolId)}/remove`, json("POST", body)),
  // This month's spend (read with the user's own Azure rights) and the tier's list price.
  serviceCost: (refresh = false) => request<ServiceCost>(`/api/apim/cost${refresh ? "?refresh=true" : ""}`),
  // Read-only: everything this app created in the service.
  cleanupPlan: () => request<RemovalPlan>("/api/apim/cleanup"),
  cleanup: (body: CleanupConfirm) => request<RemovalResult>("/api/apim/cleanup", json("POST", body)),
  apis: () => request<ApiView[]>("/api/apim/apis"),
  createPoolApi: (poolId: string, body: PoolApiCreate) =>
    request<ApiView>(`/api/apim/pools/${id(poolId)}/api`, json("POST", body)),
  policy: (apiId: string) => request<PolicyView>(`/api/apim/apis/${id(apiId)}/policy`),
  // Read-only on the server; returns the policy a bind would write.
  previewBinding: (apiId: string, body: ApiBindingWrite) =>
    request<PolicyPreview>(`/api/apim/apis/${id(apiId)}/binding/preview`, json("POST", body)),
  bind: (apiId: string, body: ApiBindingWrite) =>
    request<ApiView>(`/api/apim/apis/${id(apiId)}/binding`, json("PUT", body)),
  unbind: (apiId: string, etag?: string | null) =>
    request<void>(withEtag(`/api/apim/apis/${id(apiId)}/binding`, etag), { method: "DELETE" }),
  enableApimIdentity: () => request<SetupCheck>("/api/apim/setup/apim-identity", { method: "POST" }),
  services: (tenantId: string | null, subscriptionId: string) =>
    request<ApimServiceView[]>(`/api/apim/target/services?${scope(tenantId, subscriptionId)}`),
  createOptions: (tenantId: string | null, subscriptionId: string) =>
    request<ApimCreateOptions>(`/api/apim/target/create-options?${scope(tenantId, subscriptionId)}`),
  selectTarget: (body: TargetSelect) => request<TargetView>("/api/apim/target", json("PUT", body)),
  // Creates billable Azure resources with the user's own rights, then selects the new service.
  createService: (body: ApimCreate) => request<ApimServiceView>("/api/apim/target/services", json("POST", body)),
  // Only services this app created; Azure soft-deletes them and keeps them 48 hours.
  deleteCheck: (body: TargetSelect) =>
    request<ServiceDeleteCheck>("/api/apim/target/services/delete-check", json("POST", body)),
  deleteService: (body: TargetSelect & { confirm_name: string }) =>
    request<void>("/api/apim/target/services/delete", json("POST", body)),
  deletedServices: (tenantId: string | null, subscriptionId: string) =>
    request<DeletedServiceView[]>(`/api/apim/target/deleted?${scope(tenantId, subscriptionId)}`),
  restoreService: (body: { tenant_id: string | null; subscription_id: string; location: string; name: string }) =>
    request<ApimServiceView>("/api/apim/target/deleted/restore", json("POST", body)),
  // Subscription keys are read with the user's own Azure rights.
  apiKeys: (apiId: string) => request<SubscriptionKeyView[]>(`/api/apim/apis/${id(apiId)}/keys`),
  createApiKey: (apiId: string) => request<SubscriptionKeyView>(`/api/apim/apis/${id(apiId)}/keys`, { method: "POST" }),
  readApiKey: (apiId: string, keyId: string) =>
    request<{ primary_key: string }>(`/api/apim/apis/${id(apiId)}/keys/${id(keyId)}/secret`, { method: "POST" }),
  // The backend sends the request: browsers can't call the APIM gateway (no CORS).
  testApi: (apiId: string, body: ApiTestRequest) =>
    request<ApiTestResult>(`/api/apim/apis/${id(apiId)}/test`, json("POST", body)),
}

export const keys = {
  ...sharedKeys,
  capabilities: ["apim", "capabilities"] as const,
  backends: ["apim", "backends"] as const,
  pools: ["apim", "pools"] as const,
  pool: (poolId: string) => ["apim", "pools", poolId] as const,
  // Outside ["pools"], so refreshing the pool list never re-reads a plan nobody is looking at.
  poolRemoval: (poolId: string) => ["apim", "removal", "pool", poolId] as const,
  cleanup: ["apim", "removal", "cleanup"] as const,
  cost: ["apim", "cost"] as const,
  modelCheck: (members: string[]) => ["apim", "model-check", ...members] as const,
  apis: ["apim", "apis"] as const,
  policy: (apiId: string) => ["apim", "apis", apiId, "policy"] as const,
  apiKeys: (apiId: string) => ["apim", "apis", apiId, "keys"] as const,
  services: (tenantId: string | null, subscriptionId: string) => ["apim", "target", "services", tenantId, subscriptionId] as const,
  createOptions: (tenantId: string | null, subscriptionId: string) => ["apim", "target", "create-options", tenantId, subscriptionId] as const,
  deleted: (tenantId: string | null, subscriptionId: string) => ["apim", "target", "deleted", tenantId, subscriptionId] as const,
}

export const queries = {
  ...sharedQueries,
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
  modelCheck: (members: string[]) => queryOptions({
    queryKey: keys.modelCheck(members),
    queryFn: () => api.checkModels(members),
    enabled: members.length > 0,
    staleTime: 60_000,
  }),
  policy: (apiId: string) => queryOptions({ queryKey: keys.policy(apiId), queryFn: () => api.policy(apiId) }),
  apiKeys: (apiId: string) => queryOptions({
    queryKey: keys.apiKeys(apiId),
    queryFn: () => api.apiKeys(apiId),
    staleTime: 60_000,
    // Missing rights or no delegated token fail the same way every time; the guide explains.
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
