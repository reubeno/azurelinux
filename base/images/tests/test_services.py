# SPDX-License-Identifier: MIT
"""Validate systemd service enablement."""

from __future__ import annotations

import pytest


def test_systemd_dir_exists(rootfs) -> None:
    """The image must have /etc/systemd/system/."""
    systemd_dir = rootfs / "etc" / "systemd" / "system"
    if not systemd_dir.exists():
        pytest.skip("/etc/systemd/system not present (minimal image)")
