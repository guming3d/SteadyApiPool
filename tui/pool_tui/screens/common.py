"""Small modals shared by every view: confirm, pick from a list, prompt for text, help."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, title: str, message: str, confirm: str = "Confirm") -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm = confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self._title
            dialog.border_subtitle = "y confirm · n cancel"
            yield Static(self._message)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._confirm, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PickScreen(ModalScreen[str | None]):
    """Choose one of ``options`` (id, label); dismisses with the id, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]], hint: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._options = options
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self._title
            dialog.border_subtitle = "↑↓ choose · enter select · esc cancel"
            if self._hint:
                yield Static(self._hint, classes="muted")
            if self._options:
                yield OptionList(*(Option(label, id=key) for key, label in self._options))
            else:
                yield Static("Nothing to choose from.", classes="muted")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PromptScreen(ModalScreen[str | None]):
    """Ask for one line of text; ``pattern`` validates it before it can be submitted."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        label: str,
        value: str = "",
        *,
        pattern: re.Pattern[str] | None = None,
        invalid: str = "Not valid",
        password: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._label = label
        self._value = value
        self._pattern = pattern
        self._invalid = invalid
        self._password = password

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog") as dialog:
            dialog.border_title = self._title
            dialog.border_subtitle = "enter save · esc cancel"
            yield Label(self._label)
            yield Input(self._value, password=self._password, id="prompt")
            yield Static("", id="prompt-error", classes="error-text")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if self._pattern is not None and not self._pattern.fullmatch(value):
            self.query_one("#prompt-error", Static).update(self._invalid)
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


HELP = """\
[b]Views[/b]
  1 Pools   2 Deployments   3 Setup        q quit · ? help · ctrl+p commands

[b]Everywhere[/b]
  tab / shift+tab  move between panes      ↑↓ or j k  move in a list
  r  refresh                               esc  close a dialog

[b]Pools[/b]
  n  new pool        e  edit pool          D  delete pool
  a  create an API   b  bind an API        u  unbind the API under the cursor
  t  try the API (get a key, send a request)
  m  monitor range (1h → 6h → 24h → 7d)

[b]Pool editor[/b]
  a  add member      x  remove member      p  make primary
  + / -  weight      ctrl+s  publish       esc  cancel

[b]Deployments[/b]
  space  select      c  import and create a pool      d  deploy a model (OpenAI)
  R  rescan Azure (skips the 5-minute cache)

[b]Setup[/b]
  enter  fix the check under the cursor     c  choose the APIM service
  In choose APIM: d deletes a service this app created (type its name; Azure keeps it
  48 hours), and enter on Recently deleted restores one.

Writes go to your real APIM service. Every update is ETag-checked by the backend.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide") as dialog:
            dialog.border_title = "Keys"
            dialog.border_subtitle = "esc close"
            yield Static(HELP)

    def action_close(self) -> None:
        self.dismiss(None)
