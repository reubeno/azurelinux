# SPDX-License-Identifier: MIT
"""Validate yum/dnf repository configuration."""

from __future__ import annotations

from utils.types import RepoInfo


def test_has_repo_files(yum_repos: list[RepoInfo]) -> None:
    """At least one repo must be configured."""
    assert len(yum_repos) > 0, "No .repo files found in /etc/yum.repos.d/"
