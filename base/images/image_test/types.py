# SPDX-License-Identifier: MIT
"""Data classes used throughout the image test framework."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartitionInfo:
    """Metadata for a single disk partition."""

    device: str
    type: str  # e.g. "ext4", "vfat", "swap"
    size_bytes: int
    label: str | None = None
    mountpoint: str | None = None
    uuid: str | None = None


@dataclass(frozen=True)
class DiskInfo:
    """Aggregated disk inspection results (VM images only)."""

    partitions: list[PartitionInfo] = field(default_factory=list)
    total_size_bytes: int = 0


@dataclass(frozen=True)
class RepoInfo:
    """Parsed yum/dnf repository entry."""

    repo_id: str
    name: str
    baseurl: str | None = None
    metalink: str | None = None
    enabled: bool = True
    gpgcheck: bool = False


@dataclass(frozen=True)
class StatResult:
    """File stat information from the mounted filesystem."""

    path: str
    mode: int
    uid: int
    gid: int
    size: int
    is_dir: bool
    is_symlink: bool
    link_target: str | None = None
