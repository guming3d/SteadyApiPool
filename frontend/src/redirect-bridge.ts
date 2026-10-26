// redirect.html: where Entra sends the public app's sign-in responses. MSAL's bridge hands them to
// the page that started the sign-in (src/session.ts) and takes the browser back there.
import { broadcastResponseToMainFrame } from "@azure/msal-browser/redirect-bridge"

broadcastResponseToMainFrame().catch((error: unknown) => {
  console.error("Sign-in response could not be handed back:", error)
})
