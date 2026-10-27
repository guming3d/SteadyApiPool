"""FastAPI entry point: JSON API under ``/api`` and the built frontend at ``/``.

The routes are in routes.py (shared), apim/routes.py and litellm/routes.py.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from . import routes
from .apim import routes as apim_routes
from .config import Settings, get_settings
from .deps import (
    get_access,
    get_apim_cost,
    get_apim_monitor,
    get_apim_service,
    get_apim_targets,
    get_apim_tester,
    get_arm,
    get_benchmarks,
    get_discovery,
    get_litellm_cost,
    get_litellm_monitor,
    get_litellm_service,
    get_litellm_targets,
    get_litellm_tester,
    get_request_settings,
    get_sessions,
    get_targets,
    get_user,
    get_verifier,
)
from .errors import ServiceError
from .litellm import routes as litellm_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# azure-identity logs every token request and its headers at INFO.
logging.getLogger("azure").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"

# Re-exported: tests and tools override these factories through app.dependency_overrides.
__all__ = [
    "app",
    "get_access",
    "get_apim_cost",
    "get_apim_monitor",
    "get_apim_service",
    "get_apim_targets",
    "get_apim_tester",
    "get_arm",
    "get_benchmarks",
    "get_discovery",
    "get_litellm_cost",
    "get_litellm_monitor",
    "get_litellm_service",
    "get_litellm_targets",
    "get_litellm_tester",
    "get_request_settings",
    "get_sessions",
    "get_settings",
    "get_targets",
    "get_user",
    "get_verifier",
]

api = APIRouter(prefix="/api")
api.include_router(routes.router)
api.include_router(apim_routes.router)
api.include_router(litellm_routes.router)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # Fail fast on missing or unsafe configuration.
    if settings.auth_mode == "disabled" and settings.system_trust:
        # Before any HTTPS client exists: ARM (httpx) and sign-in (requests) both pick it up.
        import truststore

        truststore.inject_into_ssl()
    yield


app = FastAPI(title="SteadyApiPool", lifespan=lifespan)
app.include_router(api)


@app.exception_handler(ServiceError)
async def service_error(_: Request, error: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content={"detail": error.message})


# MSAL renews sign-ins in a hidden iframe that lands on this page, so only it may be framed, and
# only by this origin.
REDIRECT_PAGE = "/redirect.html"


@lru_cache
def _content_security_policy(authority: str, framed: bool) -> str:
    """Only this origin's scripts (plus the inline first-paint script, by hash), and Entra for
    signing in, so an injected script has nowhere to load from or send tokens to."""
    index = STATIC_DIR / "index.html"
    inline = (
        re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index.read_text("utf-8"), re.S)
        if index.is_file()
        else []
    )
    hashes = " ".join(
        f"'sha256-{base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()}'"
        for script in inline
    )
    login = authority.rstrip("/")
    ancestors = "'self'" if framed else "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' {hashes}".strip(),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            f"connect-src 'self' {login}",
            f"frame-src 'self' {login}",
            f"frame-ancestors {ancestors}",
            "base-uri 'self'",
            f"form-action 'self' {login}",
            "object-src 'none'",
        ]
    )


@app.middleware("http")
async def no_store(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    framed = request.url.path == REDIRECT_PAGE
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN" if framed else "DENY")
    # As the routes see it (tests override the factory).
    settings: Settings = request.app.dependency_overrides.get(get_settings, get_settings)()
    if settings.public:
        response.headers.setdefault(
            "Content-Security-Policy", _content_security_policy(settings.entra_authority, framed)
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> Response:
    if path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    candidate = (STATIC_DIR / path).resolve()
    if path and candidate.is_file() and STATIC_DIR.resolve() in candidate.parents:
        return FileResponse(candidate)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return JSONResponse(
            status_code=503, content={"detail": "Frontend not built; run npm run build"}
        )
    return FileResponse(index, headers={"Cache-Control": "no-cache"})
