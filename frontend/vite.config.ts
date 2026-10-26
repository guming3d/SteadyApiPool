import { resolve } from "node:path"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

// The build is served by FastAPI from backend/pool_manager/static, so the app and its API
// share one origin (required for the Easy Auth cookie and the write header check).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/pool_manager/static",
    emptyOutDir: true,
    // redirect.html: the public app's sign-in responses come back through it (src/session.ts).
    rolldownOptions: { input: { main: resolve(import.meta.dirname, "index.html"), redirect: resolve(import.meta.dirname, "redirect.html") } },
  },
  server: {
    port: 5174,
    proxy: { "/api": { target: process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000" } },
  },
})
