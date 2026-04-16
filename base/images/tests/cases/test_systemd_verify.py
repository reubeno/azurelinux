# SPDX-License-Identifier: MIT
"""Validate systemd unit file correctness via ``systemd-analyze verify``."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

from utils.parsers import verify_systemd_units

# Known upstream issues that are baselined.  Each entry is (unit, regex)
# where the regex matches the diagnostic text to suppress.  Only diagnostics
# matching BOTH the unit AND the pattern are suppressed — new issues in the
# same unit will still surface.
KNOWN_ISSUES: list[tuple[str, str]] = [
    # Fedora ships quotaon-root.service referencing /usr/sbin/quotaon which
    # is not installed by default.
    (
        "quotaon-root.service",
        r"Command /usr/sbin/quotaon is not executable",
    ),
    # Fedora ships rc-local.service referencing /etc/rc.d/rc.local which
    # does not exist in a clean image.
    (
        "rc-local.service",
        r"Command /etc/rc.d/rc.local is not executable",
    ),
    # Ordering cycle between systemd-fsck-root, systemd-remount-fs, and
    # dracut units.  This is a false positive: dracut units are intended for
    # the initrd phase, but systemd-analyze --root sees them alongside
    # real-root units and reports a cycle that never occurs at boot.
    (
        "systemd-fsck-root.service",
        r"Found ordering cycle",
    ),
    (
        "systemd-remount-fs.service",
        r"Found ordering cycle",
    ),
    (
        "systemd-fsck-root.service",
        r"Job .* deleted to break ordering cycle",
    ),
    (
        "systemd-remount-fs.service",
        r"Job .* deleted to break ordering cycle",
    ),
    # WALinuxAgent ships CPUAccounting= which was removed in newer systemd.
    # Tracked for fix in azurelinux.
    (
        "waagent.service",
        r"Support for option CPUAccounting= has been removed",
    ),
]


def _filter_known_issues(
    unit: str, diagnostics: str,
) -> tuple[str, list[str]]:
    """Remove diagnostic lines matching known issues for *unit*.

    Returns ``(remaining, suppressed)`` where *remaining* is the
    diagnostics after filtering and *suppressed* is the list of lines
    that matched a known-issue pattern.
    """
    lines = diagnostics.splitlines()
    filtered: list[str] = []
    suppressed: list[str] = []
    for line in lines:
        is_known = any(
            u == unit and re.search(pattern, line)
            for u, pattern in KNOWN_ISSUES
        )
        if is_known:
            suppressed.append(line)
        else:
            filtered.append(line)
    return "\n".join(filtered), suppressed


def _discover_units(rootfs: Path) -> list[str]:
    """Find all .service and .timer unit files in the image."""
    units: set[str] = set()
    for search_dir in [
        rootfs / "usr" / "lib" / "systemd" / "system",
        rootfs / "etc" / "systemd" / "system",
    ]:
        if not search_dir.is_dir():
            continue
        for f in search_dir.iterdir():
            if f.is_file() and (f.name.endswith(".service") or f.name.endswith(".timer")):
                units.add(f.name)
    return sorted(units)


def test_systemd_units_verify(rootfs: Path) -> None:
    """All systemd units in the image must pass ``systemd-analyze verify``."""
    units = _discover_units(rootfs)
    if not units:
        pytest.skip("No systemd unit files found in image")

    results = verify_systemd_units(rootfs, units)

    # Filter known issues, log what was suppressed, report genuine failures.
    failures: list[tuple[str, str]] = []
    for r in results:
        if not r.ok:
            remaining, suppressed = _filter_known_issues(r.unit, r.diagnostics)
            for line in suppressed:
                logger.info("Known issue (baselined): %s: %s", r.unit, line)
            if remaining.strip():
                failures.append((r.unit, remaining))
        

    if failures:
        summary = "\n".join(
            f"  ✗ {unit}:\n    {diag.replace(chr(10), chr(10) + '    ')}"
            for unit, diag in failures
        )
        pytest.fail(
            f"{len(failures)}/{len(results)} unit(s) failed verification:\n{summary}"
        )
