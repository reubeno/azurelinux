# SPDX-License-Identifier: MIT
"""Root conftest — fixtures for image validation.

CLI options (``--image-path``, ``--image-type``, ``--workdir``) are
registered in :mod:`utils.pytest_plugin` (loaded early via entry point).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import pytest

from utils.disk import inspect_disk
from utils.extract import (
    mount_container_image,
    mount_vm_image,
    unmount_container_image,
    unmount_vm_image,
)
from utils.parsers import (
    file_stat as _file_stat,
    parse_grub_defaults,
    parse_os_release,
    parse_repo_files,
    parse_systemd_enabled,
    query_rpm_packages,
)
from utils.pytest_plugin import detect_image_type
from utils.types import DiskInfo, PartitionInfo, RepoInfo, StatResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core fixtures (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def image_path(request: pytest.FixtureRequest) -> Path:
    p = Path(request.config.getoption("--image-path")).resolve()
    logger.info("Image path: %s", p)
    if not p.exists():
        pytest.fail(f"Image file does not exist: {p}")
    logger.debug("Image file size: %d bytes", p.stat().st_size)
    return p


@pytest.fixture(scope="session")
def image_type(request: pytest.FixtureRequest, image_path: Path) -> str:
    """``'vm'`` or ``'container'`` — from ``--image-type`` or auto-detected."""
    explicit = request.config.getoption("--image-type")
    if explicit:
        logger.info("Image type (explicit): %s", explicit)
        return explicit

    detected = detect_image_type(str(image_path))
    if detected is None:
        pytest.fail(
            f"Cannot detect image type from extension of {image_path.name}. "
            "Pass --image-type explicitly."
        )
    logger.info("Image type (auto-detected): %s", detected)
    return detected


@pytest.fixture(scope="session")
def workdir(request: pytest.FixtureRequest) -> Path:
    """Working directory for mounts and extractions."""
    explicit = request.config.getoption("--workdir")
    if explicit:
        p = Path(explicit).resolve()
    else:
        p = Path(__file__).resolve().parent / ".workdir"
    p.mkdir(parents=True, exist_ok=True)
    logger.debug("Work dir: %s", p)
    return p


@pytest.fixture(scope="session")
def rootfs(image_path: Path, image_type: str, workdir: Path) -> Path:
    """Mounted rootfs — session yield-fixture with cleanup."""
    if image_type == "vm":
        mountpoint = workdir / "vm-rootfs"
        mountpoint.mkdir(parents=True, exist_ok=True)
        logger.info("Mounting VM image at %s", mountpoint)
        mount_vm_image(image_path, mountpoint)
        yield mountpoint
        logger.info("Unmounting VM image at %s", mountpoint)
        unmount_vm_image(mountpoint)
    else:
        container_dir = workdir / "container"
        logger.info("Extracting container image to %s", container_dir)
        rootfs_path = mount_container_image(image_path, container_dir)
        logger.info("Container rootfs ready at %s", rootfs_path)
        yield rootfs_path
        logger.info("Cleaning up container extract at %s", container_dir)
        unmount_container_image(container_dir)


@pytest.fixture(scope="session")
def disk_info(image_path: Path, image_type: str) -> DiskInfo | None:
    """Partition/filesystem info — ``None`` for container images."""
    if image_type != "vm":
        logger.debug("Skipping disk inspection (not a VM image)")
        return None
    logger.info("Inspecting disk: %s", image_path)
    return inspect_disk(image_path)


# ---------------------------------------------------------------------------
# Rich parsed fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def os_release(rootfs: Path) -> dict[str, str]:
    """Parsed ``/etc/os-release``."""
    os_release_path = rootfs / "etc" / "os-release"
    logger.debug("Looking for os-release at %s", os_release_path)
    if not os_release_path.exists():
        pytest.fail("/etc/os-release not found in image")
    result = parse_os_release(os_release_path.read_text())
    logger.info("os-release: ID=%s VERSION_ID=%s", result.get("ID"), result.get("VERSION_ID"))
    logger.debug("os-release full: %s", result)
    return result


@pytest.fixture(scope="session")
def installed_packages(rootfs: Path) -> set[str]:
    """Set of installed RPM package names."""
    logger.info("Querying installed RPM packages via rpm --root")
    pkgs = query_rpm_packages(rootfs)
    logger.info("Found %d installed packages", len(pkgs))
    logger.debug("Packages: %s", sorted(pkgs))
    return pkgs


@pytest.fixture(scope="session")
def yum_repos(rootfs: Path) -> list[RepoInfo]:
    """Parsed ``/etc/yum.repos.d/*.repo`` files."""
    repo_dir = rootfs / "etc" / "yum.repos.d"
    logger.debug("Scanning repo files in %s", repo_dir)
    repos = parse_repo_files(repo_dir)
    logger.info("Found %d repos: %s", len(repos), [r.repo_id for r in repos])
    return repos


@pytest.fixture(scope="session")
def enabled_services(rootfs: Path) -> set[str]:
    """Systemd services enabled via ``*.wants/`` symlinks."""
    systemd_dir = rootfs / "etc" / "systemd"
    logger.debug("Scanning systemd enabled services in %s", systemd_dir)
    services = parse_systemd_enabled(systemd_dir)
    logger.info("Found %d enabled services: %s", len(services), sorted(services))
    return services


@pytest.fixture(scope="session")
def partition_table(
    disk_info: DiskInfo | None, image_type: str
) -> list[PartitionInfo]:
    """Partition metadata — auto-skips for container images."""
    if image_type != "vm":
        pytest.skip("partition_table not applicable to container images")
    assert disk_info is not None
    logger.info("Partition table: %d partitions", len(disk_info.partitions))
    for p in disk_info.partitions:
        logger.debug("  %s: type=%s mount=%s size=%d", p.device, p.type, p.mountpoint, p.size_bytes)
    return disk_info.partitions


@pytest.fixture(scope="session")
def kernel_cmdline(rootfs: Path, image_type: str) -> str:
    """Default kernel command line from GRUB config — auto-skips for containers."""
    if image_type != "vm":
        pytest.skip("kernel_cmdline not applicable to container images")

    grub_defaults = rootfs / "etc" / "default" / "grub"
    if grub_defaults.exists():
        logger.debug("Parsing GRUB defaults from %s", grub_defaults)
        parsed = parse_grub_defaults(grub_defaults.read_text())
        cmdline = parsed.get("GRUB_CMDLINE_LINUX_DEFAULT", "")
        logger.info("Kernel cmdline (from defaults): %s", cmdline)
        return cmdline

    # Fallback: try grub.cfg directly
    for grub_cfg_path in [
        rootfs / "boot" / "grub2" / "grub.cfg",
        rootfs / "boot" / "grub" / "grub.cfg",
    ]:
        if grub_cfg_path.exists():
            logger.debug("Parsing grub.cfg from %s", grub_cfg_path)
            content = grub_cfg_path.read_text()
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("linux") and "root=" in stripped:
                    parts = stripped.split(maxsplit=2)
                    if len(parts) >= 3:
                        logger.info("Kernel cmdline (from grub.cfg): %s", parts[2])
                        return parts[2]

    logger.warning("No kernel cmdline found in image")
    return ""


@pytest.fixture(scope="session")
def file_stat_fn(rootfs: Path) -> Callable[[str], StatResult]:
    """Callable: ``file_stat("/etc/shadow")`` → :class:`StatResult`."""

    def _stat(path: str) -> StatResult:
        logger.debug("Stat: %s", path)
        return _file_stat(rootfs, path)

    return _stat
