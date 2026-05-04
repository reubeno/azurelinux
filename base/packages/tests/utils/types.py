# SPDX-License-Identifier: MIT
"""Shared dataclasses used by fixtures, the metadata service, and tests.

Tests import these to type-annotate fixture results. They never construct
these directly — that's the metadata service's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RepoKind = Literal["binary", "srpm", "debuginfo"]
ALL_REPO_KINDS: tuple[RepoKind, ...] = ("binary", "srpm", "debuginfo")


@dataclass(frozen=True)
class NEVRA:
    """Name-Epoch-Version-Release-Arch tuple. Hashable so it can be used in sets/dicts."""

    name: str
    epoch: int
    version: str
    release: str
    arch: str

    def __str__(self) -> str:
        if self.epoch:
            return f"{self.name}-{self.epoch}:{self.version}-{self.release}.{self.arch}"
        return f"{self.name}-{self.version}-{self.release}.{self.arch}"


@dataclass(frozen=True)
class FileEntry:
    """A single file (or dir) entry from a package's filelist."""

    path: str
    is_directory: bool = False
    is_ghost: bool = False
    """True iff RPM marked the entry as ``%ghost`` — the package claims
    the path but does not actually install the file. Multiple packages
    may legitimately ``%ghost`` the same path (this is the canonical
    mechanism for non-conflicting shared file ownership)."""


@dataclass
class Package:
    """Rich package record sourced from primary repodata.

    The ``files`` attribute is populated lazily — primary metadata only
    contains a small subset of files (those marked "primary" by
    createrepo). Full file listings come from filelists metadata and
    are surfaced through the ``cross_repo_file_index`` fixture, not
    through this attribute.
    """

    nevra: NEVRA
    vendor: str | None
    sourcerpm: str | None
    summary: str | None = None
    provides: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.nevra.name

    @property
    def arch(self) -> str:
        return self.nevra.arch

    @property
    def is_source(self) -> bool:
        """True if this is a source RPM (arch is ``src`` or ``nosrc``)."""
        return self.nevra.arch in ("src", "nosrc")


@dataclass(frozen=True)
class FileOwner:
    """An owner of a file path in the cross-repo file index."""

    nevra: NEVRA
    repo_name: str
    is_directory: bool
    is_ghost: bool = False


@dataclass
class RepoclosureResult:
    """Outcome of a dnf5 repoclosure invocation against a target repo set."""

    target_repo_names: tuple[str, ...]
    arch: str
    unresolved: dict[NEVRA, list[str]] = field(default_factory=dict)
    raw_output: str = ""
    repos_by_nevra: dict[NEVRA, str] = field(default_factory=dict)
    """Per-NEVRA source repo, when the parser was able to extract it
    (JSON output only). Used by per-repo filtering in
    :func:`utils.dnf.filter_repoclosure_result`."""

    @property
    def success(self) -> bool:
        return not self.unresolved

    def __str__(self) -> str:
        if self.success:
            return (
                f"repoclosure OK for [{', '.join(self.target_repo_names)}] "
                f"on {self.arch}"
            )
        lines = [
            f"repoclosure FAILED for [{', '.join(self.target_repo_names)}] "
            f"on {self.arch}: {len(self.unresolved)} package(s) "
            "with unresolved deps:"
        ]
        for nevra, missing in sorted(self.unresolved.items(), key=lambda x: str(x[0])):
            repo = self.repos_by_nevra.get(nevra)
            suffix = f" (from {repo!r})" if repo else ""
            lines.append(f"  {nevra}{suffix}:")
            for dep in missing:
                lines.append(f"    - {dep}")
        return "\n".join(lines)
