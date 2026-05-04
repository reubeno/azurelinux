# SPDX-License-Identifier: MIT
"""Repoclosure backend abstraction.

Tests do not import this module — they consume the ``repoclosure``
fixture (in ``conftest.py``) which calls into one of these backends.

The backend interface is intentionally narrow: it exposes only
``repoclosure``. All metadata-only checks (vendor, blocklist, file
conflicts, ...) bypass dnf entirely and use
:class:`utils.metadata.MetadataService` instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..repos import Repo
from ..types import RepoclosureResult


class RepoBackend(Protocol):
    """Interface for backends that can run ``dnf5 repoclosure``."""

    def repoclosure(
        self,
        *,
        target_repos: list[Repo],
        arch: str,
        universe_repos: list[Repo] | None = None,
        check_kind: str = "binary",
    ) -> RepoclosureResult:
        """Run repoclosure against *universe_repos*, checking *target_repos*.

        * *target_repos* — repos whose packages we expect to close.
          Findings outside these repos are filtered from the result.
        * *universe_repos* — repos that contribute providers (the
          dependency universe). When ``None``, defaults to
          *target_repos* (the self-closure semantic used by the
          runtime-closure tests). For build-time closure, pass a wider
          universe (e.g., base-srpms ∪ base ∪ sdk).
        * *arch* — the target architecture (drives both the universe
          arch substitution and which package arches are checked).
        * *check_kind*:
            * ``"binary"`` — check packages of arch ∈ {*arch*,
              ``noarch``}. Pure runtime closure of binary packages.
            * ``"buildtime"`` — check packages of arch ∈ {*arch*,
              ``noarch``, ``src``, ``nosrc``}. Catches BOTH unresolved
              BuildRequires of source packages AND broken runtime
              closure of the binary packages that provide those
              BuildRequires (a broken provider would otherwise be
              silently considered a valid build dep). This is the
              correct kind for asserting an SRPM repo is buildable
              against a binary universe.
            * ``"all"`` — no arch filter; report every finding.

        Returns a :class:`RepoclosureResult` whose ``success`` is True
        iff every checked package's dependencies resolve within the
        full universe.
        """
        ...


def build_backend(
    *,
    name: str,
    workdir: Path,
    releasever: str | None,
    container_image: str,
    container_runtime: str | None,
) -> RepoBackend:
    """Construct the configured backend by name.

    Imports the concrete backend lazily so missing optional deps (e.g.
    podman not installed for the host backend path) don't break the
    other backend.
    """
    if name == "host":
        from .host import HostBackend

        return HostBackend(workdir=workdir, releasever=releasever)
    if name == "container":
        if container_runtime is None:
            raise RuntimeError(
                "container backend requires a container_runtime to be set"
            )
        from .container import ContainerBackend

        return ContainerBackend(
            workdir=workdir,
            releasever=releasever,
            image=container_image,
            runtime=container_runtime,
        )
    raise ValueError(f"unknown repoclosure backend: {name!r}")
