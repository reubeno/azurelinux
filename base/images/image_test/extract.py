# SPDX-License-Identifier: MIT
"""Image mounting/unmounting orchestration.

Uses CLI tools (guestmount, skopeo, umoci) via subprocess to avoid
system site-packages dependencies.
"""

from __future__ import annotations

import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger(__name__)


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run a command, logging it and raising with stderr on failure."""
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        logger.error(
            "Command failed (rc=%d): %s\nstdout: %s\nstderr: %s",
            result.returncode,
            " ".join(cmd),
            result.stdout,
            result.stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )
    return result


def detect_image_type(image_name: str, azldev_config: dict) -> str:
    """Determine whether *image_name* is a VM or container image.

    Looks up the image in the resolved azldev config and inspects the
    KIWI definition ``type`` attribute via the ``image`` element's
    ``image`` attribute.
    """
    images = azldev_config.get("images", {})
    image_cfg = images.get(image_name)
    if image_cfg is None:
        raise ValueError(
            f"Image '{image_name}' not found in azldev config. "
            f"Known images: {', '.join(images)}"
        )

    definition = image_cfg.get("definition", {})
    kiwi_path = definition.get("path")
    if not kiwi_path:
        raise ValueError(f"Image '{image_name}' has no definition path in config")

    # Parse the KIWI file to get the image type
    full_path = Path(kiwi_path)
    tree = ET.parse(full_path)  # noqa: S314 — trusted local file
    root = tree.getroot()

    type_elem = root.find(".//preferences/type")
    if type_elem is None:
        raise ValueError(f"No <type> element found in {kiwi_path}")

    image_attr = type_elem.get("image", "")
    if image_attr in ("oem", "vmx"):
        return "vm"
    if image_attr in ("docker", "oci"):
        return "container"

    raise ValueError(f"Unknown KIWI image type '{image_attr}' in {kiwi_path}")


# -- VM image mounting (libguestfs FUSE) ------------------------------------

# Use direct backend to avoid libvirt/SELinux issues
_GUESTFS_ENV = {**os.environ, "LIBGUESTFS_BACKEND": "direct"}


def mount_vm_image(image_path: Path, mountpoint: Path) -> Path:
    """Mount a VM image read-only via ``guestmount``.

    Returns the *mountpoint* path on success.
    """
    mountpoint.mkdir(parents=True, exist_ok=True)
    cmd = [
        "guestmount",
        "--ro",
        "-a",
        str(image_path),
        "-i",
        str(mountpoint),
    ]
    _run(cmd, env=_GUESTFS_ENV)
    return mountpoint


def unmount_vm_image(mountpoint: Path) -> None:
    """Unmount a guestmount FUSE mount."""
    logger.info("Unmounting VM image at %s", mountpoint)
    subprocess.run(
        ["guestunmount", str(mountpoint)],
        check=False,
        capture_output=True,
        text=True,
    )


# -- Container image extraction (skopeo + umoci) ---------------------------


def mount_container_image(image_path: Path, extract_dir: Path) -> Path:
    """Extract a container image rootfs using ``skopeo`` + ``umoci``.

    Converts the OCI archive to an OCI layout via ``skopeo copy``, then
    unpacks it with ``umoci unpack --rootless``.  Returns the rootfs path.
    """
    image_path = image_path.resolve()
    extract_dir = extract_dir.resolve()
    oci_layout = extract_dir / "oci-layout"
    bundle = extract_dir / "bundle"
    extract_dir.mkdir(parents=True, exist_ok=True)

    # Convert OCI archive → OCI layout
    logger.info("Converting OCI archive to layout: %s", image_path)
    _run([
        "skopeo", "copy",
        f"oci-archive:{image_path}",
        f"oci:{oci_layout}:latest",
    ])

    # Unpack into an OCI runtime bundle (rootless, no user-ns required)
    logger.info("Unpacking OCI layout to bundle: %s", bundle)
    _run([
        "umoci", "unpack", "--rootless",
        "--image", f"{oci_layout}:latest",
        str(bundle),
    ])

    rootfs = bundle / "rootfs"
    logger.info("Container rootfs at %s", rootfs)
    return rootfs


def unmount_container_image(extract_dir: Path) -> None:
    """Clean up the extracted container filesystem.

    Uses ``buildah unshare`` so that read-only directories (preserved by
    rootless ``umoci unpack``) can be removed without permission errors.
    """
    logger.info("Removing container extract dir %s", extract_dir)
    subprocess.run(
        ["buildah", "unshare", "rm", "-rf", str(extract_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
