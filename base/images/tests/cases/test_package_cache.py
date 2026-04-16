# SPDX-License-Identifier: MIT
"""Validate that package manager caches are clean in the image."""

from __future__ import annotations

from pathlib import Path

import pytest

CACHE_DIRS = [
    "/var/cache/libdnf5",
    "/var/cache/dnf",
]


@pytest.mark.parametrize("path", CACHE_DIRS)
def test_package_cache_clean(rootfs: Path, path: str) -> None:
    """Package cache directories must be empty or absent."""
    full = rootfs / path.lstrip("/")
    if not full.exists():
        return
    contents = list(full.rglob("*"))
    assert not contents, (
        f"{path} is not empty ({len(contents)} files/dirs): "
        f"{[str(p.relative_to(full)) for p in contents[:10]]}"
    )
