# SPDX-License-Identifier: MIT
"""Service layer that turns raw repodata into typed package and file
records for tests.

This is what conftest.py fixtures call into. Tests do not import this
directly.

Responsibilities:

* Memoize package lists per ``(repo.fingerprint, arch)``. The
  ``releasever`` is captured at construction time so the cache key
  doesn't need to repeat it.
* Build the cross-repo file index used by the file-conflicts test.
* Map :class:`~utils.repodata.RepodataError` raised below into
  ``pytest.fail`` calls so test output stays focused on the failing
  test rather than tracebacks from the loader.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import pytest

from .repodata import (
    RepodataError,
    RepodataLoader,
    substitute_url,
)
from .repos import Repo
from .types import FileOwner, Package

logger = logging.getLogger(__name__)


class MetadataService:
    """Caching loader for package metadata across repos.

    A single instance is created at session scope by the
    ``metadata_service`` fixture.
    """

    def __init__(self, *, workdir: Path, releasever: str | None) -> None:
        self._workdir = workdir
        self._releasever = releasever
        self._packages_cache: dict[tuple[str, str], list[Package]] = {}
        self._file_index_cache: dict[
            tuple[tuple[str, ...], str], dict[str, list[FileOwner]]
        ] = {}

    # ------------------------------------------------------------------
    # Caching helpers
    # ------------------------------------------------------------------

    def _cache_dir_for(self, repo: Repo, arch: str) -> Path:
        # The releasever isn't part of the path because a single
        # MetadataService instance has a fixed releasever; but we
        # prefix with a "rv-" segment so post-mortem inspection of a
        # workdir is unambiguous.
        rv = self._releasever or "none"
        return (
            self._workdir
            / "repodata"
            / f"rv-{rv}"
            / arch
            / f"{repo.name}-{repo.fingerprint}"
        )

    def _loader_for(self, repo: Repo, arch: str) -> RepodataLoader:
        try:
            base = substitute_url(
                repo.url, arch=arch, releasever=self._releasever
            )
        except RepodataError as exc:
            pytest.fail(str(exc))
        cache_dir = self._cache_dir_for(repo, arch)
        return RepodataLoader(base_url_substituted=base, cache_dir=cache_dir)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def list_packages(self, repo: Repo, arch: str) -> list[Package]:
        """Return every package in *repo* at *arch*. Memoized per call."""
        key = (repo.fingerprint, arch)
        if key in self._packages_cache:
            return self._packages_cache[key]
        loader = self._loader_for(repo, arch)
        try:
            packages = list(loader.packages())
        except RepodataError as exc:
            pytest.fail(
                f"failed to load primary metadata for repo {repo.name!r} "
                f"at arch {arch}: {exc}"
            )
        self._packages_cache[key] = packages
        logger.debug(
            "Loaded %d package(s) from repo %s for arch %s",
            len(packages), repo.name, arch,
        )
        return packages

    def build_file_index(
        self, repos: list[Repo], arch: str
    ) -> dict[str, list[FileOwner]]:
        """Return ``path -> [FileOwner, ...]`` for every *real* file across *repos*.

        Filtering applied before insertion into the index:

        * Directory entries (``type="dir"``) — RPM permits shared
          directory ownership.
        * Ghost entries (``type="ghost"``) — these mean "I claim this
          path but don't install it". Multiple packages can ghost the
          same path; that is the canonical mechanism for non-conflicting
          shared file ownership in RPM.

        Identical NEVRAs that appear in multiple repos contribute a
        single :class:`FileOwner` (the first repo encountered wins for
        the ``repo_name`` attribution).
        """
        key = (tuple(sorted(r.fingerprint for r in repos)), arch)
        if key in self._file_index_cache:
            return self._file_index_cache[key]

        # path -> { NEVRA -> FileOwner }, so we can dedupe by NEVRA.
        path_to_owners: dict[str, dict[object, FileOwner]] = defaultdict(dict)

        for repo in repos:
            loader = self._loader_for(repo, arch)
            try:
                for entry in loader.filelist_entries():
                    if entry.is_directory or entry.is_ghost:
                        continue
                    owner = FileOwner(
                        nevra=entry.nevra,
                        repo_name=repo.name,
                        is_directory=False,
                        is_ghost=False,
                    )
                    bucket = path_to_owners[entry.path]
                    bucket.setdefault(entry.nevra, owner)
            except RepodataError as exc:
                pytest.fail(
                    f"failed to load filelists for repo {repo.name!r} "
                    f"at arch {arch}: {exc}"
                )

        result: dict[str, list[FileOwner]] = {
            path: list(owners.values())
            for path, owners in path_to_owners.items()
        }
        self._file_index_cache[key] = result
        logger.debug(
            "Built cross-repo file index for arch %s: %d distinct path(s) "
            "(after filtering directories and ghost entries)",
            arch, len(result),
        )
        return result
