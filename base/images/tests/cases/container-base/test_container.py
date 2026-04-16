# SPDX-License-Identifier: MIT
"""Container-specific validation tests."""

from __future__ import annotations

from pathlib import Path

# Packages that should NOT be in a minimal container image
UNEXPECTED_CONTAINER_PACKAGES = {
    "kernel",
    "grub2",
    "dracut",
    "firewalld",
    "cloud-init",
}


def test_no_vm_packages(installed_packages: set[str]) -> None:
    """Container images should not contain VM-oriented packages."""
    found = UNEXPECTED_CONTAINER_PACKAGES & installed_packages
    assert not found, f"Unexpected packages in container image: {sorted(found)}"


def test_no_kernel_modules(rootfs: Path) -> None:
    """Container images should not ship kernel modules."""
    for modules_dir in [
        rootfs / "lib" / "modules",
        rootfs / "usr" / "lib" / "modules",
    ]:
        if modules_dir.exists():
            versions = list(modules_dir.iterdir())
            assert len(versions) == 0, (
                f"Container has kernel modules: {[v.name for v in versions]}"
            )
