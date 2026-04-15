# SPDX-License-Identifier: MIT
"""Pytest plugin for Azure Linux image validation.

Registered via ``[project.entry-points."pytest11"]`` so that custom CLI
options are known to pytest *before* rootdir determination.  This prevents
pytest from misinterpreting ``--image-path <existing-file>`` as a
positional test-path argument.
"""

from __future__ import annotations

# Map file-extension suffixes to image types for auto-detection.
_EXT_TO_TYPE: dict[str, str] = {
    ".raw": "vm",
    ".vhd": "vm",
    ".vhdx": "vm",
    ".vhdfixed": "vm",
    ".qcow2": "vm",
    ".oci.tar.xz": "container",
    ".tar.xz": "container",
    ".tar.gz": "container",
    ".tar": "container",
}


def detect_image_type(image_path: str) -> str | None:
    """Guess image type from *image_path* file extension."""
    lower = image_path.lower()
    # Try longest suffixes first so ".oci.tar.xz" matches before ".xz".
    for suffix in sorted(_EXT_TO_TYPE, key=len, reverse=True):
        if lower.endswith(suffix):
            return _EXT_TO_TYPE[suffix]
    return None


def pytest_addoption(parser) -> None:  # type: ignore[no-untyped-def]
    group = parser.getgroup("image", "Azure Linux image validation")
    group.addoption(
        "--image-path",
        required=True,
        help="Path to the built image artifact (VHD, raw, OCI tar.xz, etc.)",
    )
    group.addoption(
        "--image-type",
        choices=("vm", "container"),
        default=None,
        help=(
            "Image type: 'vm' or 'container'. "
            "If omitted, auto-detected from --image-path extension."
        ),
    )
    group.addoption(
        "--workdir",
        default=None,
        help=(
            "Working directory for temporary files (mounts, extractions). "
            "Defaults to .workdir/ next to conftest.py."
        ),
    )
