# SPDX-License-Identifier: MIT
"""Validate VM partition layout."""

from __future__ import annotations

import pytest

from utils.types import PartitionInfo


def test_has_partitions(partition_table: list[PartitionInfo]) -> None:
    assert len(partition_table) > 0, "No partitions found"


def test_has_root_partition(partition_table: list[PartitionInfo]) -> None:
    root_parts = [p for p in partition_table if p.mountpoint == "/"]
    assert len(root_parts) == 1, (
        f"Expected exactly one root partition, found {len(root_parts)}"
    )


def test_root_filesystem_type(partition_table: list[PartitionInfo]) -> None:
    """Root partition should use ext4."""
    root = next((p for p in partition_table if p.mountpoint == "/"), None)
    if root is None:
        pytest.skip("No root partition found")
    assert root.type == "ext4", f"Root fs type is '{root.type}', expected 'ext4'"


def test_has_efi_partition(partition_table: list[PartitionInfo]) -> None:
    """UEFI images must have a vfat EFI system partition."""
    efi_mountpoints = {"/boot/efi", "/efi"}
    efi_parts = [
        p
        for p in partition_table
        if p.mountpoint in efi_mountpoints and p.type == "vfat"
    ]
    assert len(efi_parts) >= 1, (
        "No vfat EFI partition found (expected mountpoint: /boot/efi or /efi)"
    )
