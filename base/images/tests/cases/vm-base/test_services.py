# SPDX-License-Identifier: MIT
"""Validate systemd service enablement for VM images."""

from __future__ import annotations

from typing import Callable

import pytest

EXPECTED_ENABLED = [
    "sshd.service",
    "firewalld.service",
    "systemd-networkd.service",
]

EXPECTED_NOT_ENABLED = [
    "NetworkManager.service",
]


@pytest.mark.parametrize("unit", EXPECTED_ENABLED)
def test_service_enabled(is_service_enabled: Callable[[str], bool], unit: str) -> None:
    assert is_service_enabled(unit), f"{unit} is not enabled"


@pytest.mark.parametrize("unit", EXPECTED_NOT_ENABLED)
def test_service_not_enabled(is_service_enabled: Callable[[str], bool], unit: str) -> None:
    assert not is_service_enabled(unit), f"{unit} should not be enabled"
