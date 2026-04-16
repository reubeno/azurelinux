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


def parse_systemd_enabled(etc_systemd_dir: Path) -> set[str]:
    """Walk ``*.wants/`` directories to find enabled systemd services."""
    enabled: set[str] = set()
    system_dir = etc_systemd_dir / "system"
    if not system_dir.is_dir():
        logger.debug("Systemd system dir does not exist: %s", system_dir)
        return enabled

    for wants_dir in system_dir.glob("*.wants"):
        for entry in wants_dir.iterdir():
            if entry.is_symlink() or entry.is_file():
                logger.debug("Enabled service: %s (via %s)", entry.name, wants_dir.name)
                enabled.add(entry.name)
    return enabled


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
