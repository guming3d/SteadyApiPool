"""Helpers every view and screen uses."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.dom import DOMNode

if TYPE_CHECKING:
    from .app import PoolManagerApp


def tui(node: DOMNode) -> PoolManagerApp:
    """The running app, typed: its API client, shared context and error display."""
    return cast("PoolManagerApp", node.app)
