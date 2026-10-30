// LiteLLM's part of the app, as the shared pages use it (see ../product.tsx).
import { useQuery } from "@tanstack/react-query"

import type { ProductUi } from "../product"
import { keys, queries } from "./api"
import { GatewayPicker } from "./gateway-picker"
import { PoolEditor } from "./pool-editor"
import { ServicePanel } from "./service-panel"

export const litellmUi: ProductUi = {
  product: "litellm",
  useFacts: () => {
    const capabilities = useQuery(queries.capabilities()).data
    return capabilities && {
      name: capabilities.gateway_name,
      principalId: capabilities.gateway_principal_id,
      idPrefix: capabilities.id_prefix,
      discoverySubscriptions: capabilities.discovery_subscriptions,
      // The Container App's metrics; a connected gateway has at most its traces.
      metricsUrl: capabilities.gateway_resource_id
        ? `https://portal.azure.com/#resource${capabilities.gateway_resource_id}/metrics`
        : capabilities.app_insights_id ? `https://portal.azure.com/#resource${capabilities.app_insights_id}/logs` : null,
    }
  },
  usePools: () => useQuery(queries.pools()),
  membersKey: keys.backends,
  PoolEditor,
  ServicePanel,
  Picker: GatewayPicker,
}
