# SPDX-License-Identifier: MIT
"""Snapshot test: full file manifest for distroless-minimal."""

from __future__ import annotations

from pathlib import Path

from utils.snapshot import TextSnapshotExtension


def test_file_manifest(snapshot, rootfs: Path) -> None:
    """All files in the image must match the approved manifest.

    Run with ``--snapshot-update`` to accept a new baseline after
    reviewing the diff.
    """
    files = sorted(
        str(p.relative_to(rootfs))
        for p in rootfs.rglob("*")
        if ".build-id" not in p.parts
    )
    manifest = "\n".join(files) + "\n"
    assert manifest == snapshot(extension_class=TextSnapshotExtension)
