# SPDX-License-Identifier: MIT
"""Every binary package's Release tag must end with ``.azl4`` (optionally
followed by ``~<suffix>``)."""

from __future__ import annotations

import re

import pytest

from utils.repos import Repo


# rules-as-code: edit me when the expected dist suffix changes (e.g.
# major distro version bump). The pattern allows an optional ``~<extra>``
# suffix for pre-release / hotfix builds.
RELEASE_SUFFIX_RE = re.compile(r"\.azl4(~.*)?$")


@pytest.mark.repo_kind("binary")
def test_release_suffix(repo: Repo, arch: str, repo_packages) -> None:
    """Aggregate: every non-source package's Release tag must match the suffix regex."""
    packages = repo_packages(repo, arch)
    offenders = [
        p for p in packages
        if not p.is_source and not RELEASE_SUFFIX_RE.search(p.nevra.release)
    ]
    if offenders:
        listing = "\n".join(
            f"  - {p.nevra}: release={p.nevra.release!r}" for p in offenders
        )
        pytest.fail(
            f"binary repo {repo.name!r} (arch {arch}) has "
            f"{len(offenders)} package(s) with unexpected Release suffix "
            f"(expected match: {RELEASE_SUFFIX_RE.pattern!r}):\n{listing}"
        )
