# Browser end-to-end tests

Real user journeys in a real browser (Chromium, Playwright) on the built UI, served by
`scripts/demo_server.py`: the real FastAPI app on the backend tests' in-memory Azure, APIM service
and LiteLLM gateway. Nothing here reaches Azure, and nothing is billed.

Each test resets the demo server to a stage first (`POST /demo/stage/{sign-in,ready,pool}?product=…`),
so the tests run one at a time against one server.

```sh
cd frontend && npm ci && npm run build && cd ..       # the UI the backend serves
cd e2e/browser
npm ci
npm run install-browsers                              # Chromium for Playwright, once
npm test                                              # starts the demo server itself
npm run test:headed                                   # watch it
npx playwright show-report                            # after a failure: trace, screenshot, video
```

`E2E_PYTHON` is the Python that runs the demo server (default: `backend/.venv`'s), and `E2E_PORT`
its port (default 8010). Locally a demo server already on that port is reused; on CI (`CI` set)
the tests start their own and retry a failed test once.

| File | Journeys |
| --- | --- |
| `onboarding.spec.ts` | A first run on each product: signed out, sign in, choose the kind of gateway and an existing one, reach the app |
| `litellm-gateway.spec.ts` | Deploy a LiteLLM gateway with the capacity sizer and the region check until it opens; the gateway panel (cost, resize with its warning and confirmation, switch); a pool's Metrics and the Gateway metrics tab |
| `litellm-pools.spec.ts` | Discovered deployments become a failover pool (editor, review, publish), the gateway's identity gets access, a test key, a test call names the member that answered; a pool's settings are edited, published and read back, and the pool is removed through its plan |
| `apim.spec.ts` | Deployments become an APIM pool with an API bound to it, its policy shows the binding, access, a subscription key and a test call; a pool in use: metrics, the service panel's cost and switch |
| `shell.spec.ts` | Appearance and language switches keep the page working and survive a reload |
