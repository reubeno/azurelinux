# SPDX-License-Identifier: MIT
"""Root conftest — CLI options, collection hooks, and all fixtures."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from utils.disk import inspect_disk
from utils.extract import (
    detect_image_type,
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
from utils.types import DiskInfo, PartitionInfo, RepoInfo, StatResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Collection hook — filter test directories by --image-name
# ---------------------------------------------------------------------------


def pytest_ignore_collect(
    collection_path: Path, config: pytest.Config
) -> bool | None:
    image_name = config.getoption("--image-name")
    tests_root = Path(__file__).resolve().parent

    try:
        rel = collection_path.resolve().relative_to(tests_root)
    except ValueError:
        return None

    parts = rel.parts
    if not parts:
        return None

    top_dir = parts[0]

    # Always skip the helper package
    if top_dir == "utils":
        return True

    # Inside cases/: shared tests live at cases/test_*.py, per-image in cases/<name>/
    if top_dir == "cases" and len(parts) >= 2:
        subdir = parts[1]
        # If it's a subdirectory (not a file), only collect matching image name
        candidate = tests_root / "cases" / subdir
        if candidate.is_dir() and subdir != image_name:
            return True

    return None


# ---------------------------------------------------------------------------
# Core fixtures (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def image_name(request: pytest.FixtureRequest) -> str:
    name = request.config.getoption("--image-name")
    logger.info("Image name: %s", name)
    return name


@pytest.fixture(scope="session")
def image_path(request: pytest.FixtureRequest) -> Path:
    p = Path(request.config.getoption("--image-path")).resolve()
    logger.info("Image path: %s", p)
    if not p.exists():
        pytest.fail(f"Image file does not exist: {p}")
    logger.debug("Image file size: %d bytes", p.stat().st_size)
    return p


@pytest.fixture(scope="session")
def azldev_config() -> dict:
    """Resolved TOML config from ``azldev config dump``."""
    logger.info("Loading azldev config via 'azldev config dump -q -f json'")
    result = subprocess.run(
        ["azldev", "config", "dump", "-q", "-f", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    logger.debug("Config loaded: %d top-level keys", len(config))
    return config


@pytest.fixture(scope="session")
def image_type(image_name: str, azldev_config: dict) -> str:
    """``'vm'`` or ``'container'``, detected from KIWI definition."""
    itype = detect_image_type(image_name, azldev_config)
    logger.info("Detected image type: %s", itype)
    return itype


@pytest.fixture(scope="session")
def rootfs(
    image_path: Path, image_type: str, azldev_config: dict,
) -> Path:
    """Mounted rootfs — session yield-fixture with cleanup."""
    work_dir = Path(azldev_config["project"]["workDir"])
    scratch_dir = work_dir / "scratch" / "image-tests"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Scratch dir: %s", scratch_dir)

    if image_type == "vm":
        mountpoint = scratch_dir / "vm-rootfs"
        mountpoint.mkdir(parents=True, exist_ok=True)
        logger.info("Mounting VM image at %s", mountpoint)
        mount_vm_image(image_path, mountpoint)
        yield mountpoint
        logger.info("Unmounting VM image at %s", mountpoint)
        unmount_vm_image(mountpoint)
    else:
        container_dir = scratch_dir / "container"
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
