// Pool and member IDs on AI Gateway. Pure, so node --test can import it.

// Same rules as backend/pool_manager/models.py. A pool's ID is also the model name clients send.
export const POOL_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,79}$/
// Gateway names become the first label of the gateway's host name (aigateway/models.py).
export const GATEWAY_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{1,22}[a-z0-9]$/
export const RESOURCE_GROUP_PATTERN = /^[-\w.()]{0,89}[-\w()]$/

/** A name for a new gateway, e.g. aigw-k3x9p. */
export function suggestGatewayName(random: () => number = Math.random) {
  return `aigw-${random().toString(36).slice(2, 7).padEnd(5, "0")}`
}
