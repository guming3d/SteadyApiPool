"""Omarchy's default look: Tokyo Night colours, rounded panes, quiet chrome."""

from __future__ import annotations

from textual.theme import Theme

OMARCHY = Theme(
    name="omarchy-tokyo-night",
    primary="#7aa2f7",
    secondary="#bb9af7",
    accent="#7dcfff",
    foreground="#c0caf5",
    background="#1a1b26",
    surface="#1f2335",
    panel="#24283b",
    success="#9ece6a",
    warning="#e0af68",
    error="#f7768e",
    dark=True,
    variables={
        # Unfocused pane borders and secondary text; the focused pane takes $primary.
        "pane-border": "#3b4261",
        "muted": "#737aa2",
        "block-cursor-text-style": "none",
        "footer-key-foreground": "#7aa2f7",
        "footer-background": "#1f2335",
        "input-selection-background": "#7aa2f7 35%",
    },
)
