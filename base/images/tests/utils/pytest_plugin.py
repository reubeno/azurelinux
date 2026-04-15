# SPDX-License-Identifier: MIT
"""Pytest plugin for Azure Linux image validation.

Registered via ``[project.entry-points."pytest11"]`` so that custom CLI
options are known to pytest *before* rootdir determination.  This prevents
pytest from misinterpreting ``--image-path <existing-file>`` as a
positional test-path argument.
"""

from __future__ import annotations


def pytest_addoption(parser) -> None:  # type: ignore[no-untyped-def]
    group = parser.getgroup("image", "Azure Linux image validation")
    group.addoption(
        "--image-name",
        required=True,
        help="Image name matching a key in images.toml (e.g. vm-base, container-base)",
    )
    group.addoption(
        "--image-path",
        required=True,
        help="Path to the built image artifact (VHD, tar.xz, etc.)",
    )
