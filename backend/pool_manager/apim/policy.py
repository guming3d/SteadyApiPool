"""Binds an APIM API to a backend pool by editing marker-delimited blocks in its policy.

Only the blocks between this app's markers are ever rewritten, so operator-authored
policy around them is preserved. Policies are exchanged in APIM's escaped ``xml`` format,
which is always well-formed, and every result is parsed before it is written back.

The retry block retries only 429: that lets the pool route the second attempt to a member whose
breaker has not tripped.
"""

from __future__ import annotations

import base64
import re
from xml.etree import ElementTree

from .models import (
    ApiBinding,
    ApiBindingWrite,
)

DEFAULT_POLICY = """<policies>
    <inbound>
        <base />
    </inbound>
    <backend>
        <base />
    </backend>
    <outbound>
        <base />
    </outbound>
    <on-error>
        <base />
    </on-error>
</policies>"""

# Each managed block is removed together with exactly the whitespace inserted with it, so
# unbinding restores the original policy byte for byte.
_INBOUND = re.compile(
    r"\n[ \t]*<!-- apim-backend-pool:inbound:begin -->.*?<!-- apim-backend-pool:inbound:end -->",
    re.DOTALL,
)
_BACKEND = re.compile(
    r"<backend>\s*<!-- apim-backend-pool:backend:begin original=(?P<original>[A-Za-z0-9+/=]*) -->"
    r".*?<!-- apim-backend-pool:backend:end -->\s*</backend>",
    re.DOTALL,
)
_SET_BACKEND = re.compile(r"<set-backend-service\b[^>]*>", re.DOTALL)
_BACKEND_ID = re.compile(r'backend-id="([^"]*)"')
_MI_RESOURCE = re.compile(r'<authentication-managed-identity\b[^>]*resource="([^"]*)"')
_RETRY = re.compile(
    r'<retry\b[^>]*\bcount="(?P<count>\d+)"[^>]*\binterval="(?P<interval>\d+)"'
    r'[^>]*\bfirst-fast-retry="(?P<fast>true|false)"'
)
_FORWARD_TIMEOUT = re.compile(r'<forward-request\b[^>]*\btimeout="(?P<timeout>\d+)"')
_INBOUND_OPEN = re.compile(r"<inbound\s*>")
_INBOUND_SELF_CLOSED = re.compile(r"<inbound\s*/>")
_BACKEND_SECTION = re.compile(r"<backend\s*>(?P<inner>.*?)</backend\s*>", re.DOTALL)
_BACKEND_SELF_CLOSED = re.compile(r"<backend\s*/>")
_BASE_AFTER = re.compile(r"\s*<base\s*/>")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A backend section this app may take over: empty, ``<base />``, or one plain forward-request.
_SIMPLE_BACKEND = re.compile(r"^(?:<base\s*/>|<forward-request\b[^>]*/>)?$")


class PolicyConflict(ValueError):
    pass


def read_binding(xml: str) -> tuple[ApiBinding | None, bool]:
    """Returns the managed binding, and whether another ``set-backend-service`` exists."""
    inbound = _INBOUND.search(xml)
    unmanaged = _BACKEND.sub("", _INBOUND.sub("", xml))
    foreign = bool(_SET_BACKEND.search(_COMMENT.sub("", unmanaged)))
    if inbound is None:
        return None, foreign
    block = inbound.group(0)
    backend_id = _BACKEND_ID.search(block)
    if backend_id is None:
        return None, foreign
    resource = _MI_RESOURCE.search(block)
    backend = _BACKEND.search(xml)
    retry = _RETRY.search(backend.group(0)) if backend else None
    timeout = _FORWARD_TIMEOUT.search(backend.group(0)) if backend else None
    return ApiBinding(
        pool_id=backend_id.group(1),
        retry_on_429=backend is not None,
        max_attempts_per_request=int(retry.group("count")) + 1 if retry else None,
        retry_interval_seconds=int(retry.group("interval")) if retry else None,
        first_fast_retry=retry.group("fast") == "true" if retry else None,
        backend_timeout_seconds=int(timeout.group("timeout")) if timeout else None,
        managed_identity_resource=resource.group(1) if resource else None,
    ), foreign


def _xml_attr(value: str) -> str:
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _inbound_block(binding: ApiBindingWrite) -> str:
    lines = [
        "<!-- apim-backend-pool:inbound:begin -->",
        f'<set-backend-service backend-id="{_xml_attr(binding.pool_id)}" />',
    ]
    if binding.managed_identity_resource:
        lines.append(
            "<authentication-managed-identity "
            f'resource="{_xml_attr(binding.managed_identity_resource)}" />'
        )
    lines.append("<!-- apim-backend-pool:inbound:end -->")
    return "\n        ".join(lines)


def _backend_block(binding: ApiBindingWrite, original: str) -> str:
    encoded = base64.b64encode(original.encode()).decode()
    condition = "@(context.Response != null &amp;&amp; context.Response.StatusCode == 429)"
    retry = (
        f'<retry condition="{condition}" count="{binding.max_attempts_per_request - 1}" '
        f'interval="{binding.retry_interval_seconds}" '
        f'first-fast-retry="{str(binding.first_fast_retry).lower()}">'
    )
    forward = (
        f'<forward-request timeout="{binding.backend_timeout_seconds}" '
        'buffer-request-body="true" buffer-response="false" />'
    )
    return f"""<backend>
        <!-- apim-backend-pool:backend:begin original={encoded} -->
        {retry}
            {forward}
        </retry>
        <!-- apim-backend-pool:backend:end -->
    </backend>"""


def _restore_backend(xml: str) -> str:
    def restore(match: re.Match[str]) -> str:
        return f"<backend>{base64.b64decode(match.group('original')).decode()}</backend>"

    return _BACKEND.sub(restore, xml)


def _validated(xml: str) -> str:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise PolicyConflict(f"the resulting policy is not well-formed XML: {error}") from error
    if root.tag != "policies":
        raise PolicyConflict("the API policy root element must be <policies>")
    return xml


def apply_binding(xml: str | None, binding: ApiBindingWrite) -> str:
    current = _restore_backend(_INBOUND.sub("", xml or DEFAULT_POLICY))
    if _SET_BACKEND.search(_COMMENT.sub("", current)):
        raise PolicyConflict(
            "the API policy already selects a backend with set-backend-service; "
            "remove it before binding a pool"
        )

    inbound = _inbound_block(binding)
    if _INBOUND_SELF_CLOSED.search(current):
        current = _INBOUND_SELF_CLOSED.sub(
            lambda _: f"<inbound>\n        <base />\n        {inbound}\n    </inbound>",
            current,
            count=1,
        )
    else:
        opening = _INBOUND_OPEN.search(current)
        if opening is None:
            raise PolicyConflict("the API policy has no <inbound> section")
        position = opening.end()
        base = _BASE_AFTER.match(current, position)
        if base is not None:
            position = base.end()
        current = f"{current[:position]}\n        {inbound}{current[position:]}"

    if binding.retry_on_429:
        if _BACKEND_SELF_CLOSED.search(current):
            current = _BACKEND_SELF_CLOSED.sub("<backend>\n</backend>", current, count=1)
        section = _BACKEND_SECTION.search(current)
        if section is None:
            raise PolicyConflict("the API policy has no <backend> section")
        inner = section.group("inner")
        if not _SIMPLE_BACKEND.fullmatch(_COMMENT.sub("", inner).strip()):
            raise PolicyConflict(
                "the API <backend> section contains custom logic; "
                "turn off 429 retry or add the retry manually"
            )
        current = (
            current[: section.start()] + _backend_block(binding, inner) + current[section.end() :]
        )
    return _validated(current)


def remove_binding(xml: str) -> str:
    return _validated(_restore_backend(_INBOUND.sub("", xml)))
