// Names for a new gateway. Pure, so node --test can import it. Where it runs and how big it is
// come from the capacity sizer (sizing-view.ts, backend litellm/sizing.py).

// Mirrors GATEWAY_NAME_PATTERN in pool_manager/models.py: lower case, a letter first, no
// trailing or double hyphen, 2-32 characters.
export const GATEWAY_NAME = /^[a-z](?:[a-z0-9]|-[a-z0-9]){1,30}$/

export const isGatewayName = (name: string) => name.length <= 32 && GATEWAY_NAME.test(name)

export function suggestGatewayName(random: () => number = Math.random) {
  return `ai-gateway-${random().toString(36).slice(2, 7)}`
}
