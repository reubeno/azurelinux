# SPDX-License-Identifier: MIT
"""Validate that blocklisted files are absent from the image."""

from __future__ import annotations

from pathlib import Path

import pytest

BLOCKLISTED_FILES = [
    "/etc/redhat-release",
    "/usr/lib/redhat-release",
]


@pytest.mark.parametrize("path", BLOCKLISTED_FILES)
def test_blocklisted_file_absent(rootfs: Path, path: str) -> None:
    full = rootfs / path.lstrip("/")
    assert not full.exists(), f"Blocklisted file present in image: {path}"
