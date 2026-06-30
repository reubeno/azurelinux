"""DBus shims around anaconda's BOSS / USERS / STORAGE modules."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Disk discovery
# --------------------------------------------------------------------------- #


@dataclass
class DiskInfo:
    """A discovered block device available for installation."""

    name: str         # e.g. "sda"
    path: str         # e.g. "/dev/sda"
    size_bytes: int
    model: str
    type: str         # "disk", "rom" ...
    transport: str    # "sata", "nvme", "virtio" ...

    @property
    def size_pretty(self) -> str:
        units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
        size = float(self.size_bytes)
        unit = units[0]
        for u in units:
            unit = u
            if size < 1024:
                break
            size /= 1024
        return f"{size:5.1f} {unit}"


def list_install_targets() -> List[DiskInfo]:
    """Return disks suitable to install onto, discovered via lsblk."""
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-b", "-o", "NAME,SIZE,MODEL,TYPE,TRAN,PATH"],
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        log.warning("lsblk failed: %s", exc)
        return []

    disks: List[DiskInfo] = []
    payload = json.loads(out)
    for dev in payload.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        size = int(dev.get("size") or 0)
        name = dev.get("name", "")
        # Skip zero-size phantom devices (unused nbd, empty loop, etc.).
        if size == 0:
            continue
        disks.append(
            DiskInfo(
                name=name,
                path=dev.get("path") or f"/dev/{name}",
                size_bytes=size,
                model=(dev.get("model") or "").strip() or "—",
                type=dev.get("type", ""),
                transport=(dev.get("tran") or "").strip() or "—",
            )
        )
    return disks


# --------------------------------------------------------------------------- #
# Root password
# --------------------------------------------------------------------------- #


def crypt_password(plain: str) -> str:
    """Produce a crypt(3) hash anaconda's SetCryptedRootPassword accepts."""
    from pyanaconda.core.users import crypt_password as _crypt
    return _crypt(plain)


def configure_root_password(plain: str) -> None:
    """Set the root password in anaconda's USERS module and unlock root.

    Mirrors the upstream root_password spokes:
      1. SetCryptedRootPassword(<crypt(3) hash>)
      2. IsRootAccountLocked = False
    """
    from pyanaconda.modules.common.constants.services import USERS
    proxy = USERS.get_proxy()
    proxy.SetCryptedRootPassword(crypt_password(plain))
    try:
        proxy.IsRootAccountLocked = False
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not set IsRootAccountLocked: %s", exc)


# --------------------------------------------------------------------------- #
# Localization / hostname / additional users
# --------------------------------------------------------------------------- #


def configure_language(language: str) -> None:
    """Set the system language (LANG variable). Format: 'en_US.UTF-8'."""
    from pyanaconda.modules.common.constants.services import LOCALIZATION
    LOCALIZATION.get_proxy().Language = language


def configure_timezone(tz_name: str, is_utc: bool = True) -> None:
    """Set the system timezone (e.g. 'UTC' or 'America/Los_Angeles')."""
    from pyanaconda.modules.common.constants.services import TIMEZONE
    proxy = TIMEZONE.get_proxy()
    proxy.Timezone = tz_name
    proxy.IsUTC = is_utc


def configure_hostname(hostname: str) -> None:
    """Set the system hostname."""
    from pyanaconda.modules.common.constants.services import NETWORK
    NETWORK.get_proxy().Hostname = hostname


def configure_additional_user(name: str, plain_password: str,
                              groups: Optional[List[str]] = None) -> None:
    """Create one additional user account on the installed system.

    Replaces any existing extra users. Uses anaconda's USERS.Users
    property + the UserData structure.
    """
    from pyanaconda.modules.common.constants.services import USERS
    from pyanaconda.modules.common.structures.user import UserData

    user = UserData()
    user.name = name
    user.password = crypt_password(plain_password)
    user.is_crypted = True
    user.groups = list(groups or ["wheel"])

    USERS.get_proxy().Users = UserData.to_structure_list([user])


# --------------------------------------------------------------------------- #
# Read-only helpers used to populate the Hub screen with current defaults.
# --------------------------------------------------------------------------- #


def get_current_language() -> str:
    try:
        from pyanaconda.modules.common.constants.services import LOCALIZATION
        return str(LOCALIZATION.get_proxy().Language or "en_US.UTF-8")
    except Exception:  # noqa: BLE001
        return "en_US.UTF-8"


def get_current_timezone() -> str:
    try:
        from pyanaconda.modules.common.constants.services import TIMEZONE
        return str(TIMEZONE.get_proxy().Timezone or "UTC")
    except Exception:  # noqa: BLE001
        return "UTC"


def get_current_hostname() -> str:
    try:
        from pyanaconda.modules.common.constants.services import NETWORK
        return str(NETWORK.get_proxy().Hostname or "azurelinux")
    except Exception:  # noqa: BLE001
        return "azurelinux"


def get_available_timezones() -> List[str]:
    """Return a flattened list of valid IANA timezone names."""
    try:
        from pyanaconda.modules.common.constants.services import TIMEZONE
        regions = TIMEZONE.get_proxy().GetAllValidTimezones()
        flat: List[str] = []
        for region, cities in regions.items():
            for city in cities:
                flat.append(f"{region}/{city}" if region else city)
        flat.sort()
        return flat
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not enumerate timezones: %s", exc)
        # Fallback: a sensible default set.
        return [
            "UTC",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "America/New_York",
            "Asia/Kolkata",
            "Asia/Shanghai",
            "Asia/Tokyo",
            "Australia/Sydney",
            "Europe/Berlin",
            "Europe/London",
            "Europe/Paris",
        ]


def get_common_languages() -> List[tuple[str, str]]:
    """Return a curated [(locale, display-name)] for the language picker."""
    try:
        from pyanaconda.modules.common.constants.services import LOCALIZATION
        loc = LOCALIZATION.get_proxy()
        common = loc.GetCommonLocales() or []
        # Take the locale id and its English name when available.
        out: List[tuple[str, str]] = []
        for locale_id in common:
            try:
                data = loc.GetLocaleData(locale_id)
                # Structure: {english_name, native_name, language_id, ...}
                eng = (
                    data.get("english-name")
                    or data.get("native-name")
                    or locale_id
                )
                out.append((locale_id, str(eng)))
            except Exception:  # noqa: BLE001
                out.append((locale_id, locale_id))
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not enumerate locales: %s", exc)

    return [
        ("en_US.UTF-8", "English (US)"),
        ("en_GB.UTF-8", "English (UK)"),
        ("de_DE.UTF-8", "Deutsch"),
        ("es_ES.UTF-8", "Espanol"),
        ("fr_FR.UTF-8", "Francais"),
        ("it_IT.UTF-8", "Italiano"),
        ("ja_JP.UTF-8", "Japanese"),
        ("ko_KR.UTF-8", "Korean"),
        ("pt_BR.UTF-8", "Portugues (BR)"),
        ("ru_RU.UTF-8", "Russian"),
        ("zh_CN.UTF-8", "Chinese (simplified)"),
        ("zh_TW.UTF-8", "Chinese (traditional)"),
    ]


# --------------------------------------------------------------------------- #
# Partitioning
# --------------------------------------------------------------------------- #


def apply_kickstart_partitioning() -> None:
    """Configure, validate, and apply the partitioning anaconda has built.

    The kickstart's ``clearpart`` / ``autopart`` / explicit ``part`` directives
    cause anaconda's storage module to *create* a partitioning request, but it
    is not configured/applied automatically when an interactive UI is present
    — the spoke is expected to do it. We replicate that here, mirroring
    ``pyanaconda.ui.lib.storage.apply_partitioning`` and the simpleline TUI's
    storage spoke ``execute()`` method.

    Failing to call this leaves the install task running against a
    partitioning that has never been ``ConfigureWithTask``'d, which surfaces
    inside anaconda as ``'NoneType' object has no attribute 'type'``.
    """
    from pyanaconda.modules.common.constants.services import STORAGE
    from pyanaconda.modules.common.task import sync_run_task
    from pyanaconda.modules.common.structures.validation import ValidationReport

    storage = STORAGE.get_proxy()

    # Pick the most recently created partitioning request — typically the
    # one anaconda built from the kickstart's `clearpart` + `autopart`.
    object_paths = storage.CreatedPartitioning
    if object_paths:
        partitioning_path = object_paths[-1]
        log.info("Using existing partitioning %s", partitioning_path)
    else:
        # Defensive fallback: create an automatic partitioning request.
        from pyanaconda.core.constants import PARTITIONING_METHOD_AUTOMATIC
        partitioning_path = storage.CreatePartitioning(PARTITIONING_METHOD_AUTOMATIC)
        log.info("Created new automatic partitioning %s", partitioning_path)

    partitioning = STORAGE.get_proxy(partitioning_path)

    # 1) Configure (lay out actual partitions from the request).
    cfg_task = STORAGE.get_proxy(partitioning.ConfigureWithTask())
    sync_run_task(cfg_task)
    log.info("Partitioning configured")

    # 2) Validate the resulting layout.
    val_task = STORAGE.get_proxy(partitioning.ValidateWithTask())
    sync_run_task(val_task)
    result = val_task.GetResult()
    # GetResult returns a Variant; ValidationReport.from_structure handles it.
    try:
        report = ValidationReport.from_structure(result)
    except Exception:  # noqa: BLE001
        # If the variant API differs across anaconda versions, just trust
        # the task: a failed sync_run_task would have raised already.
        report = None

    if report is not None and not report.is_valid():
        errs = "; ".join(report.error_messages) or "<no message>"
        raise RuntimeError(
            "Partitioning validation failed: " + errs
        )

    # 3) Activate the validated partitioning so the install task uses it.
    storage.ApplyPartitioning(partitioning_path)
    log.info("Partitioning applied")


# --------------------------------------------------------------------------- #
# Storage / disk selection
# --------------------------------------------------------------------------- #


def select_disks(disk_paths: List[str]) -> None:
    """Tell anaconda's STORAGE module which disks to install onto.

    anaconda expects bare device names (e.g. 'sda'), not /dev/ paths.
    """
    from pyanaconda.modules.common.constants.services import STORAGE
    from pyanaconda.modules.common.constants.objects import DISK_SELECTION

    selection = STORAGE.get_proxy(DISK_SELECTION)
    bare = [p.removeprefix("/dev/") for p in disk_paths]
    selection.SelectedDisks = bare
    log.info("Selected install disks: %s", bare)


# --------------------------------------------------------------------------- #
# Install task driver — pure polling, no GLib loop
# --------------------------------------------------------------------------- #


@dataclass
class InstallProgress:
    """A snapshot of an in-progress installation task."""

    step: int = 0
    total: int = 0
    message: str = "Starting installation…"
    done: bool = False
    error: Optional[str] = None


class InstallDriver:
    """Drives a BOSS install task and polls its progress.

    We avoid GLib signal subscriptions entirely — dasbus proxy property
    reads are thread-safe, and polling lets us run on whichever event
    loop we like (in our case the Textual asyncio loop, via a worker
    thread). This also keeps us decoupled from anaconda's own GLib
    contexts.
    """

    def __init__(self) -> None:
        self._proxy = None
        self._snapshot = InstallProgress()
        self._started = False

    def wait_for_setup_threads(self) -> None:
        """Block until anaconda's startup worker threads have finished.

        Mirrors what `installation_progress.show_all()` does in upstream
        TUI — payload / storage / subscription threads may still be busy
        when the user clicks "Install".
        """
        from pyanaconda.core.threads import thread_manager
        if thread_manager.running > 1:
            log.info(
                "Waiting for %d anaconda setup threads to finish",
                thread_manager.running - 1,
            )
            thread_manager.wait_all()

    def start(self) -> None:
        """Kick off BOSS.InstallWithTasks() and remember the task proxy."""
        from pyanaconda.core.dbus import DBus
        from pyanaconda.modules.common.constants.services import BOSS

        if self._started:
            raise RuntimeError("InstallDriver.start() called twice")

        boss = BOSS.get_proxy()
        task_path = boss.InstallWithTasks()[0]
        self._proxy = DBus.get_proxy(BOSS.service_name, task_path)
        self._proxy.Start()
        self._started = True
        log.info("Install task started at %s", task_path)

    def poll(self) -> InstallProgress:
        """Read current progress from the task proxy.

        Safe to call from any thread. Once ``IsRunning`` flips to False,
        we call ``Finish()`` once to surface any task exception, then
        mark the snapshot as ``done``.
        """
        if not self._proxy or not self._started:
            return self._snapshot

        try:
            steps = int(getattr(self._proxy, "Steps", 0) or 0)
            progress = self._proxy.Progress  # (current_step, message) tuple
            running = bool(self._proxy.IsRunning)
        except Exception as exc:  # noqa: BLE001
            log.exception("Polling install task failed")
            self._snapshot.error = f"DBus poll failed: {exc}"
            self._snapshot.done = True
            return self._snapshot

        step, message = 0, self._snapshot.message
        if isinstance(progress, (tuple, list)) and len(progress) >= 2:
            step, message = int(progress[0]), str(progress[1])
        else:
            try:
                step = int(getattr(self._proxy, "ProgressStep", 0))
                message = str(getattr(self._proxy, "ProgressMessage", message))
            except Exception:  # noqa: BLE001
                pass

        self._snapshot.step = step
        self._snapshot.total = steps
        if message:
            self._snapshot.message = message

        if not running and not self._snapshot.done:
            try:
                self._proxy.Finish()
                self._snapshot.message = "Installation complete"
            except Exception as exc:  # noqa: BLE001
                log.exception("Install task Finish() raised")
                self._snapshot.error = str(exc)
            self._snapshot.done = True

        return self._snapshot

    def run_to_completion(
        self,
        on_update: Callable[[InstallProgress], None],
        interval: float = 0.4,
    ) -> InstallProgress:
        """Blocking helper: start the task and poll until it's done."""
        self.start()
        while True:
            snap = self.poll()
            on_update(snap)
            if snap.done:
                return snap
            time.sleep(interval)
