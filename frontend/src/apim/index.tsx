// APIM's part of the app, as the shared pages use it (see ../product.tsx).
import { useQuery } from "@tanstack/react-query"

import type { ProductUi } from "../product"
import { ApimPicker } from "./apim-picker"
import { keys, queries } from "./api"
import { PoolEditor } from "./pool-editor"
import { ServicePanel } from "./service-panel"

export const apimUi: ProductUi = {
  product: "apim",
  useFacts: () => {
    const capabilities = useQuery(queries.capabilities()).data
    return capabilities && {
      name: capabilities.apim_name,
      principalId: capabilities.apim_principal_id,
      idPrefix: capabilities.id_prefix,
      discoverySubscriptions: capabilities.discovery_subscriptions,
    }
  },
  usePools: () => useQuery(queries.pools()),
  membersKey: keys.backends,
  PoolEditor,
  ServicePanel,
  Picker: ApimPicker,
}
