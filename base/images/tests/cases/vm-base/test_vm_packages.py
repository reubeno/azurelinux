# SPDX-License-Identifier: MIT
"""Validate that no development packages are installed in the VM image."""

from __future__ import annotations


def test_no_devel_packages(installed_packages: set[str]) -> None:
    """VM base images should not ship -devel packages."""
    devel = {p for p in installed_packages if p.endswith("-devel")}
    assert not devel, f"Development packages installed: {sorted(devel)}"
