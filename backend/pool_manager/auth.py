"""Caller identity: App Service Authentication (Easy Auth), or the public app's Entra tokens.

App Service strips client-supplied ``X-MS-CLIENT-PRINCIPAL-*`` headers and injects its own
only when Authentication is enabled, which the Bicep template enforces. Writes also require
a custom header: browsers cannot send it cross-site without a CORS preflight this app never
approves, which blocks CSRF against the Easy Auth session cookie.

Users listed in ``POOL_MANAGER_OWNERS`` may change pools. With ``rbac_writers`` on, so may users
whose own Azure roles already allow every APIM write the app makes; the app can't do anything
for them that they couldn't do in the portal.

The public app (``auth_mode=entra``) has no Easy Auth: the browser sends a bearer token that
deps.get_user verifies, and every Azure call runs as that user, so Azure itself decides what they
may change. The ``X-MS-*`` headers are never trusted there; a client could set them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from .access import AccessChecker, user_token
from .config import Settings, get_settings
from .deps import Guest, User, get_access
from .models import Principal

WRITE_HEADER = "X-Pool-Manager"
LOCAL_PRINCIPAL = "local-developer"
GUEST_PRINCIPAL = "guest"
GUEST_REFUSED = (
    "The guest view is read-only: sign in with your own Microsoft account to make changes, "
    "read keys or send test requests"
)


def _caller(request: Request) -> tuple[str, str]:
    return (
        request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "").strip(),
        request.headers.get("X-MS-CLIENT-PRINCIPAL-ID", "").strip(),
    )


def is_owner(request: Request, settings: Settings) -> bool:
    if settings.auth_mode != "easy_auth":
        return False
    name, object_id = _caller(request)
    owners = settings.owner_set
    return bool(name and name.casefold() in owners) or bool(
        object_id and object_id.casefold() in owners
    )


def caller_name(
    request: Request, settings: Annotated[Settings, Depends(get_settings)], user: User, guest: Guest
) -> str:
    """Who is asking, without the Azure role check ``current_principal`` may make."""
    if settings.auth_mode == "disabled":
        return LOCAL_PRINCIPAL
    if guest:
        return GUEST_PRINCIPAL
    if user is not None:
        return user.name
    name, object_id = _caller(request)
    if not name and not object_id:
        raise HTTPException(status_code=401, detail="Sign-in required")
    return name or object_id


def current_principal(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    access: Annotated[AccessChecker, Depends(get_access)],
    user: User,
    guest: Guest,
) -> Principal:
    if settings.auth_mode == "disabled":
        return Principal(name=LOCAL_PRINCIPAL, can_write=True, authenticated=False)
    if guest:
        return Principal(name=GUEST_PRINCIPAL, can_write=False, authenticated=False, guest=True)
    if user is not None:
        # Every change runs with the user's own token, so Azure decides; as locally.
        return Principal(
            name=user.name, can_write=True, authenticated=True, tenant_id=user.tenant_id
        )
    name, object_id = _caller(request)
    if not name and not object_id:
        raise HTTPException(status_code=401, detail="Sign-in required")
    can_write = is_owner(request, settings)
    if not can_write and settings.rbac_writers:
        token = user_token(request.headers)
        can_write = token is not None and not token.expired and access.user_can_write(token)
    return Principal(name=name or object_id, can_write=can_write, authenticated=True)


def acting_user(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> Principal:
    """A signed-in user acting with their own Azure rights (setup fixes, keys, test calls)."""
    if principal.guest:
        raise HTTPException(status_code=403, detail=GUEST_REFUSED)
    if request.headers.get(WRITE_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"Missing {WRITE_HEADER} header")
    return principal


def writer(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> Principal:
    if principal.guest:
        raise HTTPException(status_code=403, detail=GUEST_REFUSED)
    if request.headers.get(WRITE_HEADER) != "1":
        raise HTTPException(status_code=403, detail=f"Missing {WRITE_HEADER} header")
    if not principal.can_write:
        raise HTTPException(status_code=403, detail="Only configured owners can make changes")
    return principal
