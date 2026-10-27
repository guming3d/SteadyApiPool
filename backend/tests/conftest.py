from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pool_manager import deps


@pytest.fixture(autouse=True)
def _no_real_azure(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A dependency a test forgot to override would build a real ARM client and sign in with
    the developer's own Azure identity (az, azd, ...). Make that fail loudly instead."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError(
            "A test reached the real ARM factories in deps.py; override the dependency "
            "(see tests/apim/test_app.py::_client or tests/litellm/test_app.py::_client)"
        )

    caches = (
        deps._arm,
        deps.get_local_credential,
        deps._secrets,
        deps._targets,
        deps._apim_service,
        deps._gateway,
        deps._litellm_service,
        deps._discovery,
        deps._access,
        deps._verifier,
        deps._sessions,
    )
    for factory in caches:
        factory.cache_clear()
    monkeypatch.setattr(deps, "ArmClient", refuse)
    monkeypatch.setattr(deps, "LocalCredential", refuse)
    # The real store would read the developer's saved gateway keys.
    monkeypatch.setattr(deps, "SecretStore", refuse)
    # The public app's real token exchange would call Entra with the app's managed identity.
    monkeypatch.setattr(deps, "OboExchanger", refuse)
    yield
    for factory in caches:
        factory.cache_clear()
