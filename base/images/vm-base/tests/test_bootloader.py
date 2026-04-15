# SPDX-License-Identifier: MIT
"""Validate GRUB/bootloader configuration for VM images."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_grub_config_exists(rootfs: Path, image_type: str) -> None:
    if image_type != "vm":
        pytest.skip("Not a VM image")
    candidates = [
        rootfs / "boot" / "grub2" / "grub.cfg",
        rootfs / "boot" / "grub" / "grub.cfg",
        rootfs / "etc" / "default" / "grub",
    ]
    assert any(p.exists() for p in candidates), (
        "No GRUB configuration found in image"
    )


def test_efi_bootloader_present(rootfs: Path, image_type: str) -> None:
    """UEFI images should have an EFI boot directory."""
    if image_type != "vm":
        pytest.skip("Not a VM image")
    efi_dir = rootfs / "boot" / "efi"
    if not efi_dir.exists():
        # Some images use /efi directly
        efi_dir = rootfs / "efi"
    assert efi_dir.exists(), "No EFI boot directory found"
