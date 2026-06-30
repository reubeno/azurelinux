"""The pyanaconda.ui.UserInterface implementation backed by Textual."""
from __future__ import annotations

import logging
import sys

import meh.ui.text  # anaconda already depends on python-meh
from pyanaconda import ui
from pyanaconda.core.constants import IPMI_ABORTED, QUIT_MESSAGE
from pyanaconda.core.util import ipmi_report

from .app import InstallerApp, InstallerState

log = logging.getLogger(__name__)


class AzlTextualInterface(ui.UserInterface):
    """A modern Textual-based replacement for anaconda's simpleline TUI.

    Implements the minimum UserInterface contract anaconda requires:

    * ``setup(data)`` — wires up state and is called before run().
    * ``run()``       — blocks until the install has finished or the user
      explicitly quits.
    * ``showError`` / ``showDetailedError`` / ``showYesNoQuestion`` —
      used by anaconda's error reporting plumbing.

    Unlike ``TextUserInterface`` we do not consume anaconda's hubs/spokes;
    we collect input ourselves and drive the install via DBus
    (``BOSS.InstallWithTasks()``) just as the upstream
    ``installation_progress`` spoke does.
    """

    ENVIRONMENT = "anaconda"

    def __init__(self, storage, payload,
                 productTitle: str = "Azure Linux Installer",
                 isFinal: bool = True,
                 quitMessage: str = QUIT_MESSAGE) -> None:
        super().__init__(storage, payload)
        self.productTitle = productTitle
        self.isFinal = isFinal
        self.quitMessage = quitMessage

        # python-meh expects a TextIntf for cmdline-style error display;
        # reuse it so anaconda's exception path keeps working.
        self._meh_interface = meh.ui.text.TextIntf()

        self._app: InstallerApp | None = None
        self._state = InstallerState()
        self._data = None

    # ------------------------------------------------------------------ #
    # UserInterface contract
    # ------------------------------------------------------------------ #

    @property
    def tty_num(self) -> int:
        return 1

    @property
    def meh_interface(self):  # type: ignore[no-untyped-def]
        return self._meh_interface

    def setup(self, data) -> None:  # type: ignore[no-untyped-def]
        """Receive parsed kickstart data; record it for later use."""
        self._data = data
        log.info("AzlTextualInterface.setup() — kickstart received")

    def run(self) -> int:
        """Run the modern installer UI to completion.

        Returns 0 on a clean exit; the anaconda main loop continues and
        will trigger reboot / shutdown per the kickstart.
        """
        log.info("AzlTextualInterface.run() — starting Textual app")
        self._app = InstallerApp(state=self._state)
        try:
            result = self._app.run()
        except Exception:  # noqa: BLE001
            log.exception("Textual app crashed")
            ipmi_report(IPMI_ABORTED)
            raise

        log.info("Textual app exited with result=%r", result)

        if result == "quit":
            ipmi_report(IPMI_ABORTED)
            sys.exit(1)
        if result == "shell":
            # Use os._exit so anaconda's main loop can't preempt with the
            # kickstart's `reboot --eject` directive — the user explicitly
            # asked for a shell, they should get one.
            import os
            os._exit(2)

        # Normal completion path → fall through; anaconda will reboot via
        # the kickstart's `reboot --eject` directive.
        if self._state.install_error:
            log.error("Install reported error: %s", self._state.install_error)
            sys.exit(1)
        return 0

    # ------------------------------------------------------------------ #
    # Message helpers (used by anaconda.errors / python-meh)
    # ------------------------------------------------------------------ #

    def showError(self, message: str) -> None:  # type: ignore[override]
        # If the app is alive, surface via the active screen; otherwise
        # fall back to stderr — anaconda's flow may have aborted before
        # the UI was constructed.
        log.error("UI showError: %s", message)
        if self._app and self._app.is_running:
            self._app.call_from_thread(self._app.bell)
            self._app.call_from_thread(
                self._app.push_screen, _AlertScreen("Error", message)
            )
        else:
            print(f"\nError: {message}\n", file=sys.stderr)

    def showDetailedError(self, message: str, details: str, buttons=None) -> None:  # type: ignore[override]
        self.showError(f"{message}\n\n{details}")

    def showYesNoQuestion(self, message: str) -> bool:  # type: ignore[override]
        # We don't currently pop up interactive yes/no prompts mid-install.
        # Default to "no" — the safer choice for anaconda's recovery paths.
        log.warning("showYesNoQuestion (defaulting to No): %s", message)
        return False


# --------------------------------------------------------------------------- #
# Inline alert screen used by showError
# --------------------------------------------------------------------------- #


def _AlertScreen(title: str, message: str):  # noqa: N802  (factory naming)
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, Horizontal
    from textual.screen import ModalScreen
    from textual.widgets import Button, Static

    class _Alert(ModalScreen):
        BINDINGS = [Binding("enter", "dismiss_alert", "OK")]
        DEFAULT_CSS = """
        _Alert { align: center middle; }
        #alert-card {
            width: 80;
            border: round $error;
            padding: 1 2;
            background: $surface;
        }
        #alert-title {
            color: $error; text-style: bold; text-align: center;
        }
        #alert-msg { padding: 1 0; }
        #alert-actions { align: center middle; padding-top: 1; }
        """

        def compose(self) -> ComposeResult:
            with Container(id="alert-card"):
                yield Static(f"⚠  {title}", id="alert-title")
                yield Static(message, id="alert-msg")
                with Horizontal(id="alert-actions"):
                    yield Button("OK", variant="primary", id="alert-ok")

        def on_button_pressed(self, event):  # type: ignore[no-untyped-def]
            self.dismiss()

        def action_dismiss_alert(self) -> None:
            self.dismiss()

    return _Alert()
