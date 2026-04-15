# SPDX-License-Identifier: MIT
"""Validate file permissions and ownership on security-critical files."""

from __future__ import annotations

import stat
from typing import Callable

import pytest

from image_test.types import StatResult


def test_etc_shadow_permissions(file_stat_fn: Callable[[str], StatResult]) -> None:
    """``/etc/shadow`` must not be world-readable."""
    try:
        st = file_stat_fn("/etc/shadow")
    except FileNotFoundError:
        pytest.skip("/etc/shadow not present")
    mode = stat.S_IMODE(st.mode)
    assert not (mode & stat.S_IROTH), (
        f"/etc/shadow is world-readable (mode={oct(mode)})"
    )
    assert not (mode & stat.S_IWOTH), (
        f"/etc/shadow is world-writable (mode={oct(mode)})"
    )


def test_etc_passwd_permissions(file_stat_fn: Callable[[str], StatResult]) -> None:
    """/etc/passwd should be readable but not world-writable."""
    try:
        st = file_stat_fn("/etc/passwd")
    except FileNotFoundError:
        pytest.skip("/etc/passwd not present")
    mode = stat.S_IMODE(st.mode)
    assert not (mode & stat.S_IWOTH), (
        f"/etc/passwd is world-writable (mode={oct(mode)})"
    )


def test_etc_gshadow_permissions(file_stat_fn: Callable[[str], StatResult]) -> None:
    try:
        st = file_stat_fn("/etc/gshadow")
    except FileNotFoundError:
        pytest.skip("/etc/gshadow not present")
    mode = stat.S_IMODE(st.mode)
    assert not (mode & stat.S_IROTH), (
        f"/etc/gshadow is world-readable (mode={oct(mode)})"
    )


def test_ssh_host_key_dir_permissions(
    file_stat_fn: Callable[[str], StatResult],
) -> None:
    """``/etc/ssh`` should be 0755 or stricter."""
    try:
        st = file_stat_fn("/etc/ssh")
    except FileNotFoundError:
        pytest.skip("/etc/ssh not present")
    mode = stat.S_IMODE(st.mode)
    assert not (mode & stat.S_IWOTH), (
        f"/etc/ssh is world-writable (mode={oct(mode)})"
    )
