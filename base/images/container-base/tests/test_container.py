# SPDX-License-Identifier: MIT
"""Container-specific validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

# Packages that should NOT be in a minimal container image
UNEXPECTED_CONTAINER_PACKAGES = {
    "kernel",
    "grub2",
    "dracut",
    "firewalld",
    "cloud-init",
}


def test_minimal_package_set(
    installed_packages: set[str], image_type: str
) -> None:
    """Container images should not contain VM-oriented packages."""
    if image_type != "container":
        pytest.skip("Not a container image")
    found = UNEXPECTED_CONTAINER_PACKAGES & installed_packages
    assert not found, f"Unexpected packages in container image: {sorted(found)}"


def test_no_kernel_modules(rootfs: Path, image_type: str) -> None:
    """Container images should not ship kernel modules."""
    if image_type != "container":
        pytest.skip("Not a container image")
    for modules_dir in [
        rootfs / "lib" / "modules",
        rootfs / "usr" / "lib" / "modules",
    ]:
        if modules_dir.exists():
            versions = list(modules_dir.iterdir())
            assert len(versions) == 0, (
                f"Container has kernel modules: {[v.name for v in versions]}"
            )


def test_no_systemd_services_dir(rootfs: Path, image_type: str) -> None:
    """Minimal containers may not have systemd at all."""
    if image_type != "container":
        pytest.skip("Not a container image")
    system_dir = rootfs / "etc" / "systemd" / "system"
    # This is advisory — containers *may* have systemd, but minimal ones shouldn't
    if system_dir.exists():
        wants_dirs = list(system_dir.glob("*.wants"))
        # It's okay to have the directory, just flag if there are many enabled services
        total_enabled = sum(
            len(list(w.iterdir())) for w in wants_dirs if w.is_dir()
        )
        assert total_enabled < 10, (
            f"Container image has {total_enabled} enabled services — "
            "expected minimal or none"
        )
