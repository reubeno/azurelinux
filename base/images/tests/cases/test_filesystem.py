# SPDX-License-Identifier: MIT
"""Validate file permissions on security-critical files."""

from __future__ import annotations

import stat
from typing import Callable

import pytest

from utils.types import StatResult

# (path, forbidden_bits, description)
_PERMISSION_CHECKS = [
    ("/etc/shadow", stat.S_IROTH | stat.S_IWOTH, "must not be world-readable or world-writable"),
    ("/etc/gshadow", stat.S_IROTH | stat.S_IWOTH, "must not be world-readable or world-writable"),
    ("/etc/passwd", stat.S_IWOTH, "must not be world-writable"),
    ("/etc/ssh", stat.S_IWOTH, "must not be world-writable"),
]


@pytest.mark.parametrize(
    "path, forbidden_bits, description",
    _PERMISSION_CHECKS,
    ids=[p for p, _, _ in _PERMISSION_CHECKS],
)
def test_file_permissions(
    file_stat_fn: Callable[[str], StatResult],
    path: str,
    forbidden_bits: int,
    description: str,
) -> None:
    try:
        st = file_stat_fn(path)
    except FileNotFoundError:
        pytest.skip(f"{path} not present")
    mode = stat.S_IMODE(st.mode)
    assert not (mode & forbidden_bits), (
        f"{path} {description} (mode={oct(mode)})"
    )
