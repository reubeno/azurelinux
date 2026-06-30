"""Textual screens for the Azure Linux installer."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import (
    Center,
    Container,
    Horizontal,
    VerticalScroll,
)
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    Input,
    OptionList,
    ProgressBar,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

from . import brand
from .dbus_helpers import (
    DiskInfo,
    InstallDriver,
    InstallProgress,
    apply_kickstart_partitioning,
    configure_additional_user,
    configure_hostname,
    configure_language,
    configure_root_password,
    configure_timezone,
    get_available_timezones,
    get_common_languages,
    get_current_hostname,
    get_current_language,
    get_current_timezone,
    list_install_targets,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Azure-blue theme
# --------------------------------------------------------------------------- #


AZURE_THEME = Theme(
    name="azure",
    primary=brand.AZURE_BLUE,
    secondary=brand.AZURE_BLUE_DEEP,
    accent=brand.AZURE_CYAN,
    foreground=brand.AZURE_WHITE,
    background=brand.AZURE_NAVY,
    surface=brand.AZURE_SURFACE,
    panel=brand.AZURE_BLUE_DEEP,
    success=brand.AZURE_OK,
    warning=brand.AZURE_WARN,
    error=brand.AZURE_ERR,
    dark=True,
)


# --------------------------------------------------------------------------- #
# Shared installer state
# --------------------------------------------------------------------------- #


@dataclass
class InstallerState:
    """Plain data carried across screens."""

    # Required.
    selected_disk: Optional[DiskInfo] = None
    root_password: str = ""

    # Optional spokes — populated from kickstart-driven defaults on mount.
    language: str = "en_US.UTF-8"
    language_label: str = "English (US)"
    timezone: str = "UTC"
    hostname: str = "azurelinux"

    extra_user_name: str = ""
    extra_user_password: str = ""

    # Discovery / status.
    discovered_disks: list[DiskInfo] = field(default_factory=list)
    install_error: Optional[str] = None

    @property
    def root_password_set(self) -> bool:
        return bool(self.root_password)

    @property
    def extra_user_set(self) -> bool:
        return bool(self.extra_user_name and self.extra_user_password)

    @property
    def disk_set(self) -> bool:
        return self.selected_disk is not None

    @property
    def ready_to_install(self) -> bool:
        return self.root_password_set and self.disk_set


# --------------------------------------------------------------------------- #
# Reusable widgets
# --------------------------------------------------------------------------- #


class Banner(Static):
    """Solid-block ASCII banner."""

    DEFAULT_CSS = """
    Banner {
        color: $primary;
        text-align: center;
        padding: 1 0;
        text-style: bold;
    }
    """

    def __init__(self) -> None:
        super().__init__(brand.BANNER)


# --------------------------------------------------------------------------- #
# Welcome screen
# --------------------------------------------------------------------------- #


class WelcomeScreen(Screen):
    BINDINGS = [
        Binding("enter", "begin", "Begin"),
        Binding("q", "quit_installer", "Quit"),
    ]

    DEFAULT_CSS = """
    WelcomeScreen { align: center middle; }
    #welcome-card {
        width: 100;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #welcome-tagline {
        text-align: center;
        color: $accent;
        padding: 1 0 2 0;
    }
    #welcome-tips { padding: 1 4; color: $text-muted; }
    #welcome-actions { align: center middle; padding-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="welcome-card"):
            yield Banner()
            yield Static(
                "A modern installer for Azure Linux 4.0",
                id="welcome-tagline",
            )
            yield Static(
                "  - Offline installation from this ISO\n"
                "  - Configure system, storage and user accounts\n"
                "  - Powered by anaconda, driven by Textual",
                id="welcome-tips",
            )
            with Center(id="welcome-actions"):
                yield Button("Get started", variant="primary", id="begin-btn")
        yield Footer()

    @on(Button.Pressed, "#begin-btn")
    def _begin(self) -> None:
        self.action_begin()

    def action_begin(self) -> None:
        self.app.push_screen("hub")

    def action_quit_installer(self) -> None:
        self.app.action_quit_installer()


# --------------------------------------------------------------------------- #
# Spoke modals — small focused dialogs for each setting
# --------------------------------------------------------------------------- #


_MODAL_BASE_CSS = """
#modal-card {
    width: 80;
    height: auto;
    max-height: 90%;
    border: round $primary;
    padding: 1 2;
    background: $surface;
}
#modal-title {
    color: $primary;
    text-style: bold;
    text-align: center;
    padding-bottom: 1;
}
#modal-hint { color: $text-muted; padding-bottom: 1; }
#modal-err { color: $error; text-align: center; padding-top: 1; }
#modal-actions { align: center middle; height: 3; padding-top: 1; }
OptionList { height: auto; max-height: 12; border: round $primary; }
Input { margin-bottom: 1; height: 3; }
"""


class _CenteredModalScreen(ModalScreen):
    """ModalScreen base that horizontally + vertically centers its content.

    Textual's upstream ``ModalScreen`` sets ``layout: vertical`` and
    leaves children left-aligned. We force-center the modal-card via
    ``self.styles`` in ``__init__`` so subclasses' ``on_mount`` hooks
    don't accidentally override the alignment.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.styles.align_horizontal = "center"
        self.styles.align_vertical = "middle"


class _SpokeResult:
    """Marker for spoke results returned via push_screen_wait."""


class DiskSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Target disk", id="modal-title")
            yield Static(
                "Pick the disk to install Azure Linux onto.",
                id="modal-hint",
            )
            self.disk_list = OptionList(id="disk-list")
            yield self.disk_list
            yield Static("", id="modal-err")
            with Horizontal(id="modal-actions"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Use this disk", variant="primary", id="ok-btn")

    def on_mount(self) -> None:
        if not self._state.discovered_disks:
            self._state.discovered_disks = list_install_targets()
        sel_idx = 0
        for idx, d in enumerate(self._state.discovered_disks):
            label = f"{d.path:<14} {d.size_pretty:>10}   {d.transport:<7} {d.model}"
            self.disk_list.add_option(Option(label, id=f"disk-{idx}"))
            if self._state.selected_disk and self._state.selected_disk.path == d.path:
                sel_idx = idx
        if self._state.discovered_disks:
            self.disk_list.highlighted = sel_idx

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        idx = self.disk_list.highlighted or 0
        if 0 <= idx < len(self._state.discovered_disks):
            self._state.selected_disk = self._state.discovered_disks[idx]
            self.dismiss(True)
        else:
            self.query_one("#modal-err", Static).update("Please pick a disk.")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RootPasswordSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Root password", id="modal-title")
            yield Static(
                "Becomes the password for the 'root' user on the installed system. "
                "Minimum 6 characters.",
                id="modal-hint",
            )
            yield Input(
                placeholder="Root password",
                password=True,
                id="pw1",
                value=self._state.root_password,
            )
            yield Input(
                placeholder="Confirm root password",
                password=True,
                id="pw2",
                value=self._state.root_password,
            )
            yield Static("", id="modal-err")
            with Horizontal(id="modal-actions"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="ok-btn")

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        pw1 = self.query_one("#pw1", Input).value
        pw2 = self.query_one("#pw2", Input).value
        err = self.query_one("#modal-err", Static)
        if not pw1:
            err.update("Please enter a password.")
            return
        if pw1 != pw2:
            err.update("Passwords do not match.")
            return
        if len(pw1) < 6:
            err.update("Password must be at least 6 characters.")
            return
        self._state.root_password = pw1
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HostnameSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Hostname", id="modal-title")
            yield Static(
                "Lowercase letters, digits, and hyphens. Max 63 characters.",
                id="modal-hint",
            )
            yield Input(
                placeholder="azurelinux",
                value=self._state.hostname,
                id="hostname-input",
            )
            yield Static("", id="modal-err")
            with Horizontal(id="modal-actions"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="ok-btn")

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        v = self.query_one("#hostname-input", Input).value.strip()
        err = self.query_one("#modal-err", Static)
        if not v:
            err.update("Hostname cannot be empty.")
            return
        if len(v) > 63 or not all(c.isalnum() or c in "-." for c in v):
            err.update("Use lowercase letters, digits, dots, hyphens (max 63).")
            return
        self._state.hostname = v
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class TimezoneSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS + """
    #search-input { margin-bottom: 1; }
    """

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state
        self._all_timezones: list[str] = []

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Timezone", id="modal-title")
            yield Static(
                "Type to filter; pick an IANA zone (e.g. America/Los_Angeles).",
                id="modal-hint",
            )
            yield Input(placeholder="Search (e.g. 'london')", id="search-input")
            self.tz_list = OptionList(id="tz-list")
            yield self.tz_list
            with Horizontal(id="modal-actions"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="ok-btn")

    def on_mount(self) -> None:
        self._all_timezones = get_available_timezones()
        self._populate(self._all_timezones)

    def _populate(self, names: list[str]) -> None:
        self.tz_list.clear_options()
        sel = -1
        for i, name in enumerate(names):
            self.tz_list.add_option(Option(name, id=f"tz-{i}"))
            if name == self._state.timezone:
                sel = i
        if sel >= 0:
            self.tz_list.highlighted = sel
        elif names:
            self.tz_list.highlighted = 0
        self._filtered = names

    @on(Input.Changed, "#search-input")
    def _filter(self, event: Input.Changed) -> None:
        q = (event.value or "").strip().lower()
        if not q:
            self._populate(self._all_timezones)
            return
        self._populate([t for t in self._all_timezones if q in t.lower()])

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        idx = self.tz_list.highlighted
        if idx is None or idx < 0 or idx >= len(self._filtered):
            return
        self._state.timezone = self._filtered[idx]
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class LanguageSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state
        self._choices: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Language", id="modal-title")
            yield Static(
                "Pick the system language (LANG variable).",
                id="modal-hint",
            )
            self.lang_list = OptionList(id="lang-list")
            yield self.lang_list
            with Horizontal(id="modal-actions"):
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="ok-btn")

    def on_mount(self) -> None:
        self._choices = get_common_languages()
        sel = 0
        for i, (locale, label) in enumerate(self._choices):
            self.lang_list.add_option(
                Option(f"{label:<28} {locale}", id=f"lang-{i}")
            )
            if locale == self._state.language:
                sel = i
        if self._choices:
            self.lang_list.highlighted = sel

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        idx = self.lang_list.highlighted
        if idx is None or not (0 <= idx < len(self._choices)):
            return
        self._state.language, self._state.language_label = self._choices[idx]
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class UserSpoke(_CenteredModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = _MODAL_BASE_CSS

    def __init__(self, state: InstallerState) -> None:
        super().__init__()
        self._state = state

    def compose(self) -> ComposeResult:
        with Container(id="modal-card"):
            yield Static("Additional user (optional)", id="modal-title")
            yield Static(
                "Create a non-root account on the installed system. Will be added "
                "to the 'wheel' group (sudo-capable).",
                id="modal-hint",
            )
            yield Input(
                placeholder="Username (lowercase, no spaces)",
                value=self._state.extra_user_name,
                id="u-name",
            )
            yield Input(
                placeholder="Password",
                password=True,
                value=self._state.extra_user_password,
                id="u-pw1",
            )
            yield Input(
                placeholder="Confirm password",
                password=True,
                value=self._state.extra_user_password,
                id="u-pw2",
            )
            yield Static("", id="modal-err")
            with Horizontal(id="modal-actions"):
                yield Button("Clear / skip", id="clear-btn")
                yield Button("Cancel", id="cancel-btn")
                yield Button("Save", variant="primary", id="ok-btn")

    @on(Button.Pressed, "#cancel-btn")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#clear-btn")
    def _clear(self) -> None:
        self._state.extra_user_name = ""
        self._state.extra_user_password = ""
        self.dismiss(True)

    @on(Button.Pressed, "#ok-btn")
    def _ok(self) -> None:
        name = self.query_one("#u-name", Input).value.strip()
        pw1 = self.query_one("#u-pw1", Input).value
        pw2 = self.query_one("#u-pw2", Input).value
        err = self.query_one("#modal-err", Static)
        if not name:
            err.update("Please enter a username, or use 'Clear / skip'.")
            return
        if not name.isidentifier() or not name.islower():
            err.update("Username must be lowercase ascii letters/digits/_, "
                       "starting with a letter or _.")
            return
        if not pw1:
            err.update("Please enter a password.")
            return
        if pw1 != pw2:
            err.update("Passwords do not match.")
            return
        if len(pw1) < 6:
            err.update("Password must be at least 6 characters.")
            return
        self._state.extra_user_name = name
        self._state.extra_user_password = pw1
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# Hub screen — dashboard of spokes
# --------------------------------------------------------------------------- #


class HubScreen(Screen):
    """Anaconda-style summary hub.

    Single OptionList of "Label : current value" rows. Press Enter (or
    click) on a row to open the corresponding spoke modal. Mirrors the
    upstream simpleline TUI's summary hub layout, which is fast to
    render and easy to navigate.
    """

    BINDINGS = [
        Binding("i", "install", "Install"),
        Binding("escape", "back", "Back"),
    ]

    DEFAULT_CSS = """
    HubScreen { align: center top; }
    #hub-card {
        width: 80;
        height: auto;
        border: round $primary;
        padding: 1 2;
        margin-top: 1;
        background: $surface;
    }
    #hub-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        padding-bottom: 1;
    }
    #hub-hint { color: $text-muted; padding-bottom: 1; }
    /* OptionList auto-sizes to its 6 rows + 2 border rows = 8 rows.
       max-height caps growth in case more rows are added later. */
    #spoke-list { height: auto; max-height: 12; border: round $primary; }
    #install-info {
        color: $text-muted;
        text-align: center;
        padding-top: 1;
        height: 1;
    }
    #install-row { align: center middle; height: 3; padding-top: 1; }
    """

    # IDs map row-index -> spoke name so we know which modal to open.
    _SPOKE_ORDER = [
        "language", "timezone", "hostname",
        "disk", "rootpw", "user",
    ]

    def compose(self) -> ComposeResult:
        with Container(id="hub-card"):
            yield Static(brand.SMALL_BANNER + " - Setup overview",
                         id="hub-title")
            yield Static(
                "Use Up/Down to navigate, Enter to edit. "
                "Press 'i' (or click Install) when ready.",
                id="hub-hint",
            )
            self.spoke_list = OptionList(id="spoke-list")
            yield self.spoke_list
            yield Static("", id="install-info")
            with Horizontal(id="install-row"):
                yield Button("Back", id="back-btn")
                yield Button("Install", variant="primary", id="install-btn")
        yield Footer()

    def on_mount(self) -> None:
        state: InstallerState = self.app.state  # type: ignore[attr-defined]
        # Populate defaults from DBus (kickstart-driven where applicable).
        try:
            state.language = get_current_language()
            for loc, label in get_common_languages():
                if loc == state.language:
                    state.language_label = label
                    break
            state.timezone = get_current_timezone()
            state.hostname = get_current_hostname()
        except Exception:  # noqa: BLE001
            log.exception("Reading current settings from DBus failed")

        if not state.discovered_disks:
            state.discovered_disks = list_install_targets()
        if state.selected_disk is None and len(state.discovered_disks) == 1:
            state.selected_disk = state.discovered_disks[0]

        self._populate()

    def on_screen_resume(self) -> None:
        # After dismissing a spoke modal we come back here; refresh values.
        self._populate()

    def _populate(self) -> None:
        state: InstallerState = self.app.state  # type: ignore[attr-defined]

        def row(label: str, value: str, required: bool = False,
                ok: bool = True) -> str:
            color = "$accent" if ok else "$warning"
            tag = " [bold $primary][REQUIRED][/]" if required and not ok else ""
            # Two-column row: 20-char label, value in color.
            return f"  [bold]{label:<18}[/]: [{color}]{value}[/]{tag}"

        disk = state.selected_disk
        rows = [
            row("Language", state.language_label or state.language),
            row("Timezone", state.timezone),
            row("Hostname", state.hostname),
            row(
                "Target disk",
                f"{disk.path}  ({disk.size_pretty}, {disk.model})"
                if disk else "not selected",
                required=True, ok=disk is not None,
            ),
            row(
                "Root password",
                "set" if state.root_password_set else "not set",
                required=True, ok=state.root_password_set,
            ),
            row(
                "Additional user",
                state.extra_user_name if state.extra_user_set
                else "none (optional)",
            ),
        ]

        # Remember which option was highlighted so navigation doesn't jump
        # back to the top after every spoke close.
        prev = self.spoke_list.highlighted
        self.spoke_list.clear_options()
        for i, text in enumerate(rows):
            self.spoke_list.add_option(Option(text, id=f"row-{i}"))
        self.spoke_list.highlighted = prev if prev is not None else 0

        # Status line + install button gating.
        info = self.query_one("#install-info", Static)
        install_btn = self.query_one("#install-btn", Button)
        if state.ready_to_install:
            info.update("[green]Ready to install.[/]")
            install_btn.disabled = False
        else:
            missing = []
            if not state.disk_set:
                missing.append("target disk")
            if not state.root_password_set:
                missing.append("root password")
            info.update(f"Required: {', '.join(missing)}")
            install_btn.disabled = True

    @on(OptionList.OptionSelected, "#spoke-list")
    def _on_row(self, event: OptionList.OptionSelected) -> None:
        if not event.option_id or not event.option_id.startswith("row-"):
            return
        idx = int(event.option_id.split("-", 1)[1])
        spoke = self._SPOKE_ORDER[idx]
        modal_cls = {
            "language": LanguageSpoke,
            "timezone": TimezoneSpoke,
            "hostname": HostnameSpoke,
            "disk":     DiskSpoke,
            "rootpw":   RootPasswordSpoke,
            "user":     UserSpoke,
        }[spoke]
        self.app.push_screen(modal_cls(self.app.state),
                             callback=lambda _: self._populate())

    @on(Button.Pressed, "#back-btn")
    def _back(self) -> None:
        self.action_back()

    @on(Button.Pressed, "#install-btn")
    def _install(self) -> None:
        self.action_install()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_install(self) -> None:
        state: InstallerState = self.app.state  # type: ignore[attr-defined]
        if state.ready_to_install:
            self.app.push_screen("progress")


# --------------------------------------------------------------------------- #
# Progress screen — drives the actual install
# --------------------------------------------------------------------------- #


class ProgressScreen(Screen):
    BINDINGS = []  # quit is also disabled while we're mounted

    DEFAULT_CSS = """
    ProgressScreen { align: center top; }
    #progress-card {
        width: 110;
        height: auto;
        border: round $primary;
        padding: 1 2;
        margin-top: 1;
        background: $surface;
    }
    #progress-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        height: 1;
    }
    #progress-step {
        text-align: center;
        color: $accent;
        padding: 1 0;
        height: 3;
    }
    ProgressBar { width: 100%; height: 1; }
    #install-log {
        height: 14;
        border: round $primary;
        margin-top: 1;
    }
    """

    current_step: reactive[str] = reactive("Preparing...")

    def compose(self) -> ComposeResult:
        with Container(id="progress-card"):
            yield Static("Installing Azure Linux", id="progress-title")
            yield Static(self.current_step, id="progress-step")
            self.progress_bar = ProgressBar(total=100, show_eta=False)
            yield self.progress_bar
            self.log_widget = RichLog(highlight=True, markup=True, id="install-log")
            yield self.log_widget
        yield Footer()

    def on_mount(self) -> None:
        self.app.installing = True  # type: ignore[attr-defined]
        self._worker = threading.Thread(target=self._run_install, daemon=False)
        self._worker.start()

    def watch_current_step(self, msg: str) -> None:
        try:
            self.query_one("#progress-step", Static).update(msg)
        except Exception:  # noqa: BLE001
            pass

    def _run_install(self) -> None:
        import traceback
        state: InstallerState = self.app.state  # type: ignore[attr-defined]
        driver = InstallDriver()
        try:
            self._post(
                self._log_line,
                f"[cyan]>[/] Target disk: "
                f"{state.selected_disk.path if state.selected_disk else '(kickstart default)'}",
            )
            self._post(self._log_line, "[cyan]>[/] Pushing settings to anaconda modules...")
            configure_language(state.language)
            configure_timezone(state.timezone, is_utc=(state.timezone == "UTC"))
            configure_hostname(state.hostname)
            configure_root_password(state.root_password)
            if state.extra_user_set:
                configure_additional_user(state.extra_user_name,
                                          state.extra_user_password)

            self._post(self._log_line, "[cyan]>[/] Waiting for anaconda startup threads...")
            driver.wait_for_setup_threads()

            self._post(self._log_line, "[cyan]>[/] Configuring and applying partitioning...")
            apply_kickstart_partitioning()

            self._post(self._log_line, "[cyan]>[/] Starting installation tasks...")

            def _on_update(snap: InstallProgress) -> None:
                self._post(self._apply_snapshot, snap)

            final = driver.run_to_completion(_on_update)
            state.install_error = final.error
            self._post(
                self._log_line,
                ("[red]Error: [/]" + final.error) if final.error
                else "[green]Installation finished[/]",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Install driver failed")
            tb = traceback.format_exc()
            state.install_error = f"{exc}\n\nTraceback:\n{tb}"
            self._post(self._log_line, f"[red]Error: {exc}[/]")
            try:
                with open("/tmp/azl-installer.log", "a") as f:
                    f.write(f"\n--- install failure ---\n{tb}\n")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._post(self._goto_complete)

    def _post(self, fn, *args, **kwargs) -> None:
        try:
            self.app.call_from_thread(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001
            log.exception("call_from_thread failed for %s", fn)

    def _log_line(self, line: str) -> None:
        self.log_widget.write(line)

    def _apply_snapshot(self, snap: InstallProgress) -> None:
        if snap.total > 0:
            self.progress_bar.total = snap.total
            self.progress_bar.progress = snap.step
        self.current_step = snap.message
        self._log_line(f"[dim]step {snap.step}/{snap.total or '?'}[/] {snap.message}")

    def _goto_complete(self) -> None:
        self.app.installing = False  # type: ignore[attr-defined]
        self.app.push_screen("complete")


# --------------------------------------------------------------------------- #
# Completion screen
# --------------------------------------------------------------------------- #


class CompleteScreen(Screen):
    BINDINGS = [
        Binding("enter", "reboot", "Reboot"),
        Binding("s", "shell", "Drop to shell"),
    ]

    DEFAULT_CSS = """
    CompleteScreen { align: center middle; }
    #complete-card {
        width: 110;
        height: auto;
        max-height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    #complete-title {
        text-align: center;
        text-style: bold;
        padding-bottom: 1;
        height: 2;
    }
    /* Scrollable body that auto-sizes up to ~20 rows so a long traceback
       can still scroll; a short "success" message keeps the card compact. */
    #complete-body-scroll {
        height: auto;
        max-height: 20;
        border: round $primary;
        padding: 1;
    }
    #complete-body { text-align: left; height: auto; }
    #complete-actions { align: center middle; height: 3; padding-top: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="complete-card"):
            yield Static("", id="complete-title")
            with VerticalScroll(id="complete-body-scroll"):
                yield Static("", id="complete-body")
            with Horizontal(id="complete-actions"):
                yield Button("Reboot", variant="primary", id="reboot-btn")
                yield Button("Drop to shell", id="shell-btn")
        yield Footer()

    def on_show(self) -> None:
        state: InstallerState = self.app.state  # type: ignore[attr-defined]
        title = self.query_one("#complete-title", Static)
        body = self.query_one("#complete-body", Static)
        if state.install_error:
            title.update("[red]Installation failed[/]")
            body.update(state.install_error + "\n\nLogs: /tmp/anaconda.log")
        else:
            title.update("[green]Azure Linux installed successfully[/]")
            body.update(
                "The installation completed without errors.\n"
                "Remove the installation media and reboot to start the new system."
            )

    @on(Button.Pressed, "#reboot-btn")
    def _reboot(self) -> None:
        self.action_reboot()

    @on(Button.Pressed, "#shell-btn")
    def _shell(self) -> None:
        self.action_shell()

    def action_reboot(self) -> None:
        self.app.exit(result="reboot")

    def action_shell(self) -> None:
        self.app.exit(result="shell")


# --------------------------------------------------------------------------- #
# Root app
# --------------------------------------------------------------------------- #


class InstallerApp(App):
    """Main Textual application for the Azure Linux installer."""

    TITLE = "Azure Linux Installer"
    SUB_TITLE = "anaconda . Textual frontend"

    SCREENS = {
        "welcome": WelcomeScreen,
        "hub": HubScreen,
        "progress": ProgressScreen,
        "complete": CompleteScreen,
    }

    BINDINGS = [
        Binding("ctrl+c", "quit_installer", "Quit", show=False, priority=True),
    ]

    def __init__(self, state: Optional[InstallerState] = None) -> None:
        super().__init__()
        self.state = state or InstallerState()
        self.installing = False

    def on_mount(self) -> None:
        self.register_theme(AZURE_THEME)
        self.theme = "azure"
        self.push_screen("welcome")

    def action_quit_installer(self) -> None:
        if self.installing:
            self.bell()
            return
        self.exit(result="quit")
