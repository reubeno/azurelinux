# SPDX-License-Identifier: MIT
"""Every binary package must carry the expected Vendor tag."""

from __future__ import annotations

import pytest

from utils.repos import Repo


# rules-as-code: edit me if the expected vendor changes.
EXPECTED_VENDOR = "Microsoft Corporation"


@pytest.mark.repo_kind("binary")
def test_binary_packages_have_expected_vendor(
    repo: Repo, arch: str, repo_packages
) -> None:
    """Aggregate: every non-source package must have Vendor == EXPECTED_VENDOR."""
    packages = repo_packages(repo, arch)
    offenders = [
        p for p in packages
        if not p.is_source and (p.vendor or "").strip() != EXPECTED_VENDOR
    ]
    if offenders:
        listing = "\n".join(
            f"  - {p.nevra}: vendor={(p.vendor or '<none>')!r}"
            for p in offenders
        )
        pytest.fail(
            f"binary repo {repo.name!r} (arch {arch}) has "
            f"{len(offenders)} package(s) with unexpected Vendor "
            f"(expected {EXPECTED_VENDOR!r}):\n{listing}"
        )
