# SPDX-License-Identifier: MIT
"""File content parsers for image validation.

Each parser takes raw file content (or a filesystem path) and returns
structured Python objects.
"""

from __future__ import annotations

import configparser
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .tools import NativeTool
from .types import RepoInfo, StatResult

logger = logging.getLogger(__name__)

REQUIRED_TOOLS = [
    NativeTool(
        name="rpm",
        package_hint="rpm",
        reason="query installed packages via rpm --root",
        when="always",
    ),
    NativeTool(
        name="systemctl",
        package_hint="systemd",
        reason="query unit enablement state via systemctl --root",
        when="always",
    ),
    NativeTool(
        name="systemd-analyze",
        package_hint="systemd",
        reason="verify unit file correctness via systemd-analyze --root",
        when="always",
    ),
]


def parse_os_release(content: str) -> dict[str, str]:
    """Parse ``/etc/os-release`` KEY=VALUE format into a dict.

    Handles quoted and unquoted values per the os-release spec.
    """
    result: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip matching quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    logger.debug("Parsed os-release: %d keys", len(result))
    return result


def parse_repo_files(repo_dir: Path) -> list[RepoInfo]:
    """Parse all ``*.repo`` files in a directory into :class:`RepoInfo` list."""
    repos: list[RepoInfo] = []
    if not repo_dir.is_dir():
        logger.debug("Repo dir does not exist: %s", repo_dir)
        return repos

    for repo_file in sorted(repo_dir.glob("*.repo")):
        logger.debug("Parsing repo file: %s", repo_file.name)
        repos.extend(_parse_single_repo_file(repo_file.read_text()))
    logger.debug("Parsed %d repos from %s", len(repos), repo_dir)
    return repos


def _parse_single_repo_file(content: str) -> list[RepoInfo]:
    """Parse a single INI-style ``.repo`` file."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(content)

    repos: list[RepoInfo] = []
    for section in parser.sections():
        repos.append(
            RepoInfo(
                repo_id=section,
                name=parser.get(section, "name", fallback=section),
                baseurl=parser.get(section, "baseurl", fallback=None),
                metalink=parser.get(section, "metalink", fallback=None),
                enabled=parser.getboolean(section, "enabled", fallback=True),
                gpgcheck=parser.getboolean(section, "gpgcheck", fallback=False),
            )
        )
    return repos


def is_service_enabled(rootfs: Path, unit: str) -> bool:
    """Fast check whether a single systemd unit is enabled.

    Uses ``systemctl --root is-enabled`` which resolves only the
    specified unit's symlink chain — much faster than listing all units.
    """
    cmd = [
        "systemctl", "--root", str(rootfs),
        "is-enabled", unit, "--no-pager",
    ]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    state = result.stdout.strip()
    logger.debug("is-enabled %s → %s (rc=%d)", unit, state, result.returncode)
    return state == "enabled"


def query_systemd_unit_files(rootfs: Path) -> dict[str, str]:
    """Query systemd unit enablement states via ``systemctl --root``.

    Returns a dict mapping unit name to its state (e.g. ``"enabled"``,
    ``"disabled"``, ``"static"``, ``"masked"``, ``"indirect"``).

    .. note:: This scans *all* unit files and can be slow on FUSE-mounted
       filesystems.  Prefer :func:`is_service_enabled` for checking
       individual units.
    """
    cmd = [
        "systemctl", "--root", str(rootfs),
        "list-unit-files", "--no-pager", "--no-legend",
    ]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.warning(
            "systemctl list-unit-files failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
        return {}

    units: dict[str, str] = {}
    for line in result.stdout.splitlines():
        # Format: "<unit>  <vendor-preset>  <runtime-state>"  or  "<unit>  <state>"
        parts = line.split()
        if len(parts) >= 2:
            unit_name, state = parts[0], parts[1]
            units[unit_name] = state
    logger.debug("systemctl returned %d unit files", len(units))
    return units


def query_enabled_services(rootfs: Path) -> set[str]:
    """Return the set of systemd units that are ``enabled``.

    .. note:: Calls :func:`query_systemd_unit_files` which scans all units.
       For checking individual units, prefer :func:`is_service_enabled`.
    """
    units = query_systemd_unit_files(rootfs)
    enabled = {name for name, state in units.items() if state == "enabled"}
    logger.debug("Enabled services: %s", sorted(enabled))
    return enabled


def query_masked_services(rootfs: Path) -> set[str]:
    """Return the set of systemd units that are ``masked``."""
    units = query_systemd_unit_files(rootfs)
    masked = {name for name, state in units.items() if state == "masked"}
    logger.debug("Masked services: %s", sorted(masked))
    return masked


def parse_grub_defaults(content: str) -> dict[str, str]:
    """Parse ``/etc/default/grub`` into a key-value dict."""
    result: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        result[key] = value
    return result


def find_kernel_cmdline(rootfs: Path) -> str:
    """Extract the default kernel command line from a mounted rootfs.

    Tries ``/etc/default/grub`` first, then falls back to parsing
    ``grub.cfg`` for a ``linux`` line.  Returns ``""`` if nothing found.
    """
    grub_defaults = rootfs / "etc" / "default" / "grub"
    if grub_defaults.exists():
        logger.debug("Parsing GRUB defaults from %s", grub_defaults)
        parsed = parse_grub_defaults(grub_defaults.read_text())
        cmdline = parsed.get("GRUB_CMDLINE_LINUX_DEFAULT", "")
        if cmdline:
            logger.debug("Kernel cmdline (from defaults): %s", cmdline)
            return cmdline

    for grub_cfg_path in [
        rootfs / "boot" / "grub2" / "grub.cfg",
        rootfs / "boot" / "grub" / "grub.cfg",
    ]:
        if grub_cfg_path.exists():
            logger.debug("Parsing grub.cfg from %s", grub_cfg_path)
            for line in grub_cfg_path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith("linux") and "root=" in stripped:
                    parts = stripped.split(maxsplit=2)
                    if len(parts) >= 3:
                        logger.debug("Kernel cmdline (from grub.cfg): %s", parts[2])
                        return parts[2]

    logger.debug("No kernel cmdline found in image")
    return ""


def query_rpm_packages(rootfs: Path) -> set[str]:
    """Query installed RPM packages via ``rpm --root``.

    Raises :class:`RuntimeError` if the query fails (e.g. missing rpmdb).
    """
    cmd = ["rpm", "--root", str(rootfs), "-qa", "--qf", "%{NAME}\n"]
    logger.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"rpm query failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    pkgs = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    logger.debug("rpm query returned %d packages", len(pkgs))
    return pkgs


def file_stat(rootfs: Path, path: str) -> StatResult:
    """Stat a file relative to the mounted rootfs."""
    full_path = rootfs / path.lstrip("/")
    if not full_path.exists() and not full_path.is_symlink():
        raise FileNotFoundError(f"{path} not found in image")

    st = full_path.lstat()
    link_target = None
    if full_path.is_symlink():
        link_target = os.readlink(full_path)

    return StatResult(
        path=path,
        mode=st.st_mode,
        uid=st.st_uid,
        gid=st.st_gid,
        size=st.st_size,
        is_dir=full_path.is_dir(),
        is_symlink=full_path.is_symlink(),
        link_target=link_target,
    )


# ---------------------------------------------------------------------------
# systemd-analyze
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitVerifyResult:
    """Result of ``systemd-analyze verify`` for a single unit."""

    unit: str
    ok: bool
    diagnostics: str


def verify_systemd_units(rootfs: Path, units: list[str]) -> list[UnitVerifyResult]:
    """Run ``systemd-analyze verify --root`` on units.

    Verifies all units in a single batch invocation to amortize the cost
    of loading the unit dependency graph (expensive on FUSE mounts).
    Diagnostics are then attributed back to individual units.
    """
    if not units:
        return []

    cmd = [
        "systemd-analyze", "verify",
        "--root", str(rootfs),
        "--man=no",
        *units,
    ]
    logger.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stderr = proc.stderr

    # Attribute each diagnostic line to its unit.  Lines typically start
    # with the unit path or name, e.g.:
    #   /root/.../sshd.service:16: Some warning
    #   foo.service: Some error
    unit_diags: dict[str, list[str]] = {u: [] for u in units}
    for line in stderr.splitlines():
        matched = False
        for u in units:
            # Match unit name at start of line (with or without path prefix)
            if u in line.split(":", 1)[0] if ":" in line else u in line:
                unit_diags[u].append(line)
                matched = True
                break
        if not matched and line.strip():
            # Unattributed diagnostic — attach to a special key
            unit_diags.setdefault("_unattributed", []).append(line)

    results: list[UnitVerifyResult] = []
    for u in units:
        diag = "\n".join(unit_diags.get(u, []))
        ok = not diag
        if not ok:
            logger.debug("verify %s: FAIL\n%s", u, diag)
        else:
            logger.debug("verify %s: OK", u)
        results.append(UnitVerifyResult(unit=u, ok=ok, diagnostics=diag))

    return results
