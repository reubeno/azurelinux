# SPDX-License-Identifier: MIT
"""Validate /etc/os-release fields."""

from __future__ import annotations


def test_os_release_has_required_keys(os_release: dict[str, str]) -> None:
    """os-release must contain the distro-identifying keys."""
    for key in ("NAME", "ID", "VERSION_ID"):
        assert key in os_release, f"Missing required key: {key}"


def test_os_release_id(os_release: dict[str, str]) -> None:
    assert os_release.get("ID") == "azurelinux"


def test_os_release_version(os_release: dict[str, str]) -> None:
    assert os_release.get("VERSION_ID") == "4.0"


def test_os_release_name(os_release: dict[str, str]) -> None:
    name = os_release.get("NAME", "")
    assert "Azure Linux" in name
