# SPDX-License-Identifier: MIT
"""In-process repoclosure using ``hawkey`` (libsolv).

This module replaces the previous backend abstraction (``host`` and
``container`` flavours that shelled out to ``dnf5 repoclosure``) with
a single in-process implementation. Because ``hawkey`` is the same
library ``dnf`` uses internally for solver work, the semantics match
``dnf repoclosure``'s own "for each package, check that every
``Requires:`` has a provider in the loaded sack" — but without the
subprocess plumbing, the JSON-vs-text output schema-drift handling,
the ``--json`` capability probe, the bind-mount/SELinux relabel
gymnastics, or the host-vs-container split that existed only because
older host ``dnf5`` lacked ``--json``.

Tests do not import this directly — they consume the ``repoclosure``
fixture in ``conftest.py``.
"""

from __future__ import annotations

import logging
import warnings
from typing import TYPE_CHECKING

import pytest

from .repos import Repo
from .types import NEVRA, RepoclosureResult

if TYPE_CHECKING:
    from .metadata import MetadataService

logger = logging.getLogger(__name__)


# Suppress hawkey's "use dnf.repo.Repo instead" DeprecationWarning. We
# deliberately use ``hawkey.Repo`` because it lets us point the sack
# at already-downloaded metadata files without pulling in the full
# dnf.Base initialisation cost (which is what dnf.repo.Repo expects).
# The deprecation has been "scheduled for 2019-12-31" since libdnf
# 0.x and shows no sign of actually happening; the warning is just
# noise in our test reports.
warnings.filterwarnings(
    "ignore",
    message=".*hawkey.Repo is deprecated.*",
    category=DeprecationWarning,
)


# ---------------------------------------------------------------------------
# Arch-set for the configured check_kind
# ---------------------------------------------------------------------------


_ARCH_SETS_BY_KIND: dict[str, tuple[str, ...]] = {
    # Each entry is the set of *additional* arches (beyond the test's
    # target arch) that the checker should examine.
    #
    # "binary"    — pure runtime closure of binary packages. Only
    #               packages of arch ∈ {target, noarch} are checked;
    #               source-arch packages are ignored.
    # "buildtime" — checks BOTH source packages (so an SRPM's
    #               BuildRequires must close) AND the binary packages
    #               that satisfy them (so the binary providers'
    #               runtime deps must also close — without this, a
    #               broken provider would silently still be considered
    #               a "valid" BuildRequires source). This is the
    #               correct kind for asserting that an SRPM repo is
    #               actually buildable against a binary universe.
    # "all"       — no arch filter; report findings for every package
    #               in the universe.
    "binary": ("noarch",),
    "buildtime": ("noarch", "src", "nosrc"),
    "all": (),
}


def _arches_to_check(check_kind: str, arch: str) -> set[str] | None:
    """Return the arches whose packages should be checked, or None for "all"."""
    if check_kind not in _ARCH_SETS_BY_KIND:
        raise ValueError(
            f"unknown check_kind: {check_kind!r}; "
            f"expected one of {sorted(_ARCH_SETS_BY_KIND)}"
        )
    if check_kind == "all":
        return None
    extras = _ARCH_SETS_BY_KIND[check_kind]
    return {arch, *extras}


# ---------------------------------------------------------------------------
# Repoclosure runner
# ---------------------------------------------------------------------------


# Requires that the solver pretends are real but that no actual
# package can satisfy. ``rpmlib(...)`` is the canonical example
# (encodes runtime capabilities of the rpm tool itself); dnf
# repoclosure also ignores them.
def _is_synthetic_dep(name: str) -> bool:
    return name.startswith("rpmlib(") or name == "solvable:prereqmarker"


class Repoclosure:
    """Run hawkey-based repoclosure against a set of repos.

    Holds a reference to the :class:`MetadataService` so it can reuse
    the same on-disk metadata cache the metadata-only tests already
    populated (no double fetch).
    """

    def __init__(self, metadata_service: "MetadataService") -> None:
        self._metadata = metadata_service

    def _build_sack(
        self, repos: list[Repo], arch: str
    ) -> "tuple[object, dict[str, str]]":
        """Build a hawkey ``Sack`` containing every repo's metadata.

        Returns ``(sack, repo_name_by_id)``. The id->name map is used
        to translate ``pkg.reponame`` (which is the *hawkey* repo id
        we set when loading) back into our :class:`Repo` name.
        """
        # Imported lazily so module import doesn't fail in environments
        # that have no hawkey installed (e.g. doc builds).
        import hawkey

        sack = hawkey.Sack(arch=arch, make_cache_dir=False)
        # Hawkey doesn't have a session-cache concept the way dnf
        # does; loading from the librepo destdir is direct.
        name_by_id: dict[str, str] = {}
        for repo in repos:
            layout = self._metadata.fetch(repo, arch)
            hk_repo = hawkey.Repo(repo.name)
            hk_repo.repomd_fn = str(layout.repomd)
            hk_repo.primary_fn = str(layout.primary)
            hk_repo.filelists_fn = str(layout.filelists)
            # ``load_filelists=True`` makes file-path Requires
            # (e.g. ``Requires: /usr/bin/python3``) resolvable via
            # filelists, matching dnf's default behaviour.
            sack.load_repo(hk_repo, load_filelists=True)
            name_by_id[repo.name] = repo.name
        return sack, name_by_id

    def run(
        self,
        *,
        target_repos: list[Repo],
        arch: str,
        universe_repos: list[Repo] | None = None,
        check_kind: str = "binary",
    ) -> RepoclosureResult:
        """Run repoclosure and return the typed result.

        See :func:`utils.conftest.repoclosure` for argument semantics.
        """
        if not target_repos:
            raise ValueError("target_repos must be non-empty")
        if universe_repos is None:
            universe_repos = target_repos
        universe_names = {r.name for r in universe_repos}
        missing_targets = [r.name for r in target_repos if r.name not in universe_names]
        if missing_targets:
            raise ValueError(
                f"target_repos {missing_targets} are not in universe_repos "
                f"{sorted(universe_names)}"
            )

        import hawkey

        sack, _ = self._build_sack(universe_repos, arch)
        check_arches = _arches_to_check(check_kind, arch)

        target_names = tuple(r.name for r in target_repos)
        # For "buildtime" we deliberately do NOT filter findings to
        # ``target_repos`` — see test_repoclosure_base_srpms_buildtime
        # for the rationale (we MUST surface broken runtime closure
        # of binary providers from non-target repos that satisfy a
        # checked SRPM's BuildRequires; otherwise the check is moot).
        # For other kinds we filter to packages whose owning repo is
        # in ``target_repos``.
        target_filter = (
            None if check_kind == "buildtime" else {r.name for r in target_repos}
        )

        # Walk every checked package and verify each Requires has a
        # provider in the loaded sack.
        unresolved: dict[NEVRA, list[str]] = {}
        repos_by_nevra: dict[NEVRA, str] = {}

        all_pkgs = hawkey.Query(sack).run()
        for pkg in all_pkgs:
            if check_arches is not None and pkg.arch not in check_arches:
                continue
            if target_filter is not None and pkg.reponame not in target_filter:
                continue
            missing: list[str] = []
            for req in pkg.requires:
                req_str = str(req)
                if _is_synthetic_dep(req_str):
                    continue
                if not hawkey.Query(sack).filter(provides=req).run():
                    missing.append(req_str)
            if not missing:
                continue
            nevra = NEVRA(
                name=pkg.name,
                epoch=int(pkg.epoch),
                version=pkg.version,
                release=pkg.release,
                arch=pkg.arch,
            )
            unresolved[nevra] = missing
            repos_by_nevra[nevra] = pkg.reponame

        result = RepoclosureResult(
            target_repo_names=target_names,
            arch=arch,
            unresolved=unresolved,
            repos_by_nevra=repos_by_nevra,
        )
        logger.debug(
            "repoclosure(%s, arch=%s, kind=%s): %d unresolved package(s)",
            target_names, arch, check_kind, len(unresolved),
        )
        return result


def make_repoclosure(metadata_service: "MetadataService") -> Repoclosure:
    """Construct a :class:`Repoclosure`. Surfaces a clear error if hawkey is missing."""
    try:
        import hawkey  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        pytest.fail(
            "hawkey (python3-hawkey) is not installed; repoclosure tests "
            f"cannot run. Install your distro's python3-hawkey package. ({exc})"
        )
    return Repoclosure(metadata_service)
