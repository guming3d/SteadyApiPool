// Every gateway product the app manages. The shell and the setup wizard pick from these; the
// rest of the shared code reaches the chosen one through ProductProvider (product.tsx).
import { apimUi } from "./apim"
import { litellmUi } from "./litellm"
import type { ProductUi } from "./product"
import type { Product } from "./types"

export const PRODUCTS: Record<Product, ProductUi> = { apim: apimUi, litellm: litellmUi }
