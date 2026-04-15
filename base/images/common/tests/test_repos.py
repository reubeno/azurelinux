# SPDX-License-Identifier: MIT
"""Validate yum/dnf repository configuration."""

from __future__ import annotations

from pathlib import Path

from image_test.types import RepoInfo


def test_repos_dir_exists(rootfs: Path) -> None:
    """The image must have a yum.repos.d directory."""
    assert (rootfs / "etc" / "yum.repos.d").is_dir()


def test_has_repo_files(yum_repos: list[RepoInfo]) -> None:
    """At least one repo must be configured."""
    assert len(yum_repos) > 0, "No .repo files found in /etc/yum.repos.d/"
