// What the shared pages need from the chosen gateway's product. apim/index.tsx and
// litellm/index.tsx each provide one; products.ts lists them, and the app shell puts the chosen
// one in context for everything below it.
import { createContext, useContext, type ComponentType, type ReactNode } from "react"
import type { QueryKey, UseQueryResult } from "@tanstack/react-query"

import type { PoolMember, Product, SetupIdentity, TargetView } from "./types"

/** What the Pools tab and the deployments page show of a pool; both products' pools have it. */
export type PoolSummary = { id: string; title: string | null; members: PoolMember[]; protected: boolean }

/** The chosen gateway, from the product's capabilities. */
export type GatewayFacts = {
  name: string
  /** The managed identity the gateway calls Foundry as; null until it has one. */
  principalId: string | null
  /** Prefix for the IDs this app gives pools and members. */
  idPrefix: string
  discoverySubscriptions: string[]
}

/** Prefills a new pool, e.g. from imported deployments. */
export type PoolCreateRequest = { id: string; title: string; members: string[] }
/** Opens an existing pool with these members added to the draft, e.g. imported deployments. */
export type PoolAddRequest = { poolId: string; members: string[] }

/** What the Pools tab gives a product's pool editor. `F` is what an editor wants done once the
 *  pool it just created opens (APIM: create its API), which it hands to onCreated. */
export type PoolEditorProps<F = unknown> = {
  /** Omit to create a new pool. */
  poolId?: string
  initial?: PoolCreateRequest
  /** Members to add to an existing pool's draft once it loads; publishing stays manual. */
  addMembers?: string[]
  /** Set when the pool was just created in the create dialog, with its follow-up. */
  created?: { next?: F }
  onCreatedHandled?: () => void
  onCreated?: (poolId: string, next?: F) => void
  onDeleted?: () => void
  header?: (controls: ReactNode) => ReactNode
}

export type ProductUi = {
  product: Product
  /** The gateway's facts; undefined while its capabilities load (or can't be read yet). */
  useFacts: () => GatewayFacts | undefined
  usePools: () => UseQueryResult<PoolSummary[]>
  /** The members' query: refetched after deployments are imported as members. */
  membersKey: QueryKey
  // Each editor has its own follow-up type; the Pools tab only hands it back.
  PoolEditor: ComponentType<PoolEditorProps<any>>
  /** The gateway panel behind the gateway's name in the top bar. */
  ServicePanel: ComponentType<{ onClose: () => void }>
  /** Setup's gateway step: choose, create or delete this product's gateways. */
  Picker: ComponentType<{ target: TargetView; user: SetupIdentity | null }>
}

const ProductContext = createContext<ProductUi | null>(null)

export function ProductProvider({ ui, children }: { ui: ProductUi; children: ReactNode }) {
  return <ProductContext value={ui}>{children}</ProductContext>
}

/** The chosen gateway's product; only below the shell's ProductProvider. */
export function useProductUi(): ProductUi {
  const ui = useContext(ProductContext)
  if (!ui) throw new Error("useProductUi needs a ProductProvider")
  return ui
}

/** The chosen target as one product's picker sees it: the other product's gateway is none of
 *  its own, but its tenant and subscription are still where to start looking. */
export function targetFor(product: Product, target: TargetView): TargetView {
  if (target.product === product) return target
  return { ...target, gateway_name: null, gateway_kind: null, resource_group: null, gateway_url: null, cluster_id: null, provisioning: false }
}
