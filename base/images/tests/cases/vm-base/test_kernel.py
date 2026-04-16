# SPDX-License-Identifier: MIT
"""Validate kernel command line and module configuration for VM images."""

from __future__ import annotations

from pathlib import Path


def test_serial_console_configured(kernel_cmdline: str) -> None:
    """VM images should have serial console enabled for Azure."""
    assert "console=ttyS0" in kernel_cmdline, (
        f"console=ttyS0 not found in kernel cmdline: {kernel_cmdline!r}"
    )


def test_kernel_installed(rootfs: Path) -> None:
    """Image must have at least one kernel version installed."""
    modules_dir = rootfs / "usr" / "lib" / "modules"
    if not modules_dir.exists():
        modules_dir = rootfs / "lib" / "modules"
    assert modules_dir.exists(), "No kernel modules directory found"
    versions = [d.name for d in modules_dir.iterdir() if d.is_dir()]
    assert len(versions) >= 1, "No kernel version directories found"
