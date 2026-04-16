# SPDX-License-Identifier: MIT
"""Snapshot test: installed package manifest for container-base."""

from __future__ import annotations

from utils.snapshot import TextSnapshotExtension


def test_package_manifest(snapshot, installed_packages: set[str]) -> None:
    """Installed package names must match the approved manifest.

    Run with ``--snapshot-update`` to accept a new baseline after
    reviewing the diff.
    """
    manifest = "\n".join(sorted(installed_packages)) + "\n"
    assert manifest == snapshot(extension_class=TextSnapshotExtension)
