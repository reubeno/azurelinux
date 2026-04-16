# SPDX-License-Identifier: MIT
"""Validate /etc/os-release fields."""

from __future__ import annotations

from datetime import date


def test_os_release_has_required_keys(os_release: dict[str, str]) -> None:
    """os-release must contain the distro-identifying keys."""
    for key in ("NAME", "ID", "VERSION_ID"):
        assert key in os_release, f"Missing required key: {key}"


def test_os_release_id(os_release: dict[str, str]) -> None:
    assert os_release.get("ID") == "azurelinux"


def test_os_release_id_like(os_release: dict[str, str]) -> None:
    assert os_release.get("ID_LIKE") == "fedora"


def test_os_release_version(os_release: dict[str, str]) -> None:
    assert os_release.get("VERSION_ID") == "4.0"


def test_os_release_name(os_release: dict[str, str]) -> None:
    name = os_release.get("NAME", "")
    assert "Azure Linux" in name


def test_os_release_support_end(os_release: dict[str, str]) -> None:
    """SUPPORT_END must be present and in the future."""
    raw = os_release.get("SUPPORT_END")
    assert raw, "SUPPORT_END not set in os-release"
    support_end = date.fromisoformat(raw)
    assert support_end > date.today(), (
        f"SUPPORT_END ({raw}) is in the past — image is end-of-life"
    )
