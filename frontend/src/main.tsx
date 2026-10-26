import { StrictMode, type ReactNode } from "react"
import { createRoot } from "react-dom/client"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { ApiError } from "./api"
import { App } from "./app"
import { SignInPage } from "./components/sign-in-page"
import { LocaleProvider } from "./i18n"
import { isGuest, isPublic, signedIn, startSession } from "./session"
import "./styles.css"

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      // A 4xx is the server's considered answer; only retry what could plausibly recover.
      retry: (failureCount, error) =>
        failureCount < 1 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
    },
  },
})

const render = (page: ReactNode) => createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <LocaleProvider>
        {page}
      </LocaleProvider>
    </QueryClientProvider>
  </StrictMode>,
)

// The public app asks who you are first; locally and with Easy Auth the page is already signed in.
startSession().then(
  () => render(isPublic() && !signedIn() && !isGuest() ? <SignInPage /> : <App />),
  (error: unknown) => render(isPublic() ? <SignInPage error={error} /> : <App />),
)
