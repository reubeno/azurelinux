# SPDX-License-Identifier: MIT
"""Test-facing fixture surface for Azure Linux repo validation.

This module is the single place tests touch. They never reach below
into ``utils.metadata``, ``utils.repodata``, or ``utils.backends.*``
directly — those are implementation. To wire new data into a test,
extend the relevant service and add (or extend) a fixture here.

CLI options, markers, and the parametrize-time fan-out across
``(repo, arch)`` pairs all live in :mod:`utils.pytest_plugin`.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from utils.backends import RepoBackend, build_backend
from utils.metadata import MetadataService
from utils.repos import Repo
from utils.types import FileOwner, Package, RepoclosureResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session-scoped basics
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def workdir(request: pytest.FixtureRequest) -> Generator[Path, None, None]:
    """Working directory for repo metadata caches and dnf state.

    If ``--workdir`` is set, the directory is reused as-is and never
    removed (post-mortem friendly). Otherwise a fresh temp directory
    is created and cleaned up at session end.
    """
    explicit = request.config.getoption("azl_workdir")
    if explicit:
        p = Path(explicit).resolve()
        p.mkdir(parents=True, exist_ok=True)
        logger.debug("Workdir (explicit, will not be removed): %s", p)
        yield p
        return

    p = Path(tempfile.mkdtemp(prefix="azl-repo-tests-"))
    logger.debug("Workdir (temp, will be removed at session end): %s", p)
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


@pytest.fixture(scope="session")
def releasever(request: pytest.FixtureRequest) -> str | None:
    """Effective ``$releasever`` for this run, or ``None`` if not set."""
    return getattr(request.config, "_azl_releasever", None)


@pytest.fixture(scope="session")
def expected_vendor(request: pytest.FixtureRequest) -> str:
    """The expected RPM Vendor: tag (driven by ``--expected-vendor``)."""
    return getattr(
        request.config, "_azl_expected_vendor", "Microsoft Corporation"
    )


@pytest.fixture(scope="session")
def release_suffix_pattern(request: pytest.FixtureRequest) -> str:
    """The expected Release-tag regex (driven by ``--release-suffix``)."""
    return getattr(
        request.config, "_azl_release_suffix", r"\.azl4(~.*)?$"
    )


@pytest.fixture(scope="session")
def all_repos(request: pytest.FixtureRequest) -> list[Repo]:
    """Every repo passed via ``--repo`` (in input order)."""
    return list(getattr(request.config, "_azl_repos", []))


@pytest.fixture(scope="session")
def binary_repos(all_repos: list[Repo]) -> list[Repo]:
    """All ``binary`` repos passed via ``--repo``."""
    return [r for r in all_repos if r.kind == "binary"]


@pytest.fixture(scope="session")
def srpm_repos(all_repos: list[Repo]) -> list[Repo]:
    """All ``srpm`` repos passed via ``--repo``."""
    return [r for r in all_repos if r.kind == "srpm"]


@pytest.fixture(scope="session")
def debuginfo_repos(all_repos: list[Repo]) -> list[Repo]:
    """All ``debuginfo`` repos passed via ``--repo``."""
    return [r for r in all_repos if r.kind == "debuginfo"]


# ---------------------------------------------------------------------------
# Service layer fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def metadata_service(workdir: Path, releasever: str | None) -> MetadataService:
    """Service that loads repo packages and file lists from repodata.

    Tests should not call this directly — use the higher-level
    fixtures (``repo_packages``, ``all_binary_packages``,
    ``cross_repo_file_index``) instead. It's exposed here only so that
    those fixtures (and any future ones) can share a single, caching
    instance for the session.
    """
    return MetadataService(workdir=workdir, releasever=releasever)


@pytest.fixture(scope="session")
def backend(request: pytest.FixtureRequest, workdir: Path, releasever: str | None) -> RepoBackend:
    """The configured ``RepoBackend`` (host or container) for ``repoclosure``.

    Tests should not call this directly — use the ``repoclosure``
    fixture instead.
    """
    config = request.config
    backend_name: str = config.getoption("azl_repoclosure_backend")
    container_image: str = config.getoption("azl_container_image")
    container_runtime: str | None = getattr(
        config, "_azl_container_runtime", None
    )
    return build_backend(
        name=backend_name,
        workdir=workdir,
        releasever=releasever,
        container_image=container_image,
        container_runtime=container_runtime,
    )


# ---------------------------------------------------------------------------
# High-level test-facing fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_packages(metadata_service: MetadataService):
    """Return a callable ``(repo, arch) -> list[Package]``.

    Memoized inside :class:`MetadataService` per ``(repo, arch,
    releasever)``, so calling it from many tests in one session is
    cheap.
    """
    def _load(repo: Repo, arch: str) -> list[Package]:
        return metadata_service.list_packages(repo, arch)

    return _load


@pytest.fixture
def all_binary_packages(metadata_service: MetadataService, binary_repos: list[Repo]):
    """Return a callable ``arch -> dict[Repo, list[Package]]`` over all binary repos."""
    def _load(arch: str) -> dict[Repo, list[Package]]:
        return {r: metadata_service.list_packages(r, arch) for r in binary_repos}

    return _load


@pytest.fixture
def cross_repo_file_index(
    metadata_service: MetadataService, binary_repos: list[Repo]
):
    """Return a callable ``arch -> dict[path, list[FileOwner]]``.

    Only binary repos contribute; directories are filtered out by the
    metadata service. Identical NEVRAs that appear in multiple repos
    are deduped (one ``FileOwner`` per unique NEVRA).
    """
    def _load(arch: str) -> dict[str, list[FileOwner]]:
        return metadata_service.build_file_index(binary_repos, arch)

    return _load


@pytest.fixture
def repoclosure(backend: RepoBackend):
    """Return a callable that runs repoclosure.

    Signature::

        repoclosure(
            target_repos,           # repos whose packages we expect to close
            arch,                   # target architecture
            *,
            universe_repos=None,    # wider universe (defaults to target_repos)
            check_kind="binary",    # "binary" | "buildtime" | "all"
        ) -> RepoclosureResult

    See :meth:`utils.backends.base.RepoBackend.repoclosure` for the
    semantics of each argument.
    """
    def _run(
        target_repos: list[Repo],
        arch: str,
        *,
        universe_repos: list[Repo] | None = None,
        check_kind: str = "binary",
    ) -> RepoclosureResult:
        return backend.repoclosure(
            target_repos=target_repos,
            arch=arch,
            universe_repos=universe_repos,
            check_kind=check_kind,
        )

    return _run


# ---------------------------------------------------------------------------
# Helper available to tests that have hard-coded repo expectations.
# ---------------------------------------------------------------------------


@pytest.fixture
def require_named_repos(all_repos: list[Repo]):
    """Helper used by hard-coded tests like ``test_repoclosure_base_plus_sdk``.

    Given a list of expected repo names, returns the matching
    :class:`Repo` objects. Behavior:

    * If **all** are provided: returns them in input order.
    * If **none** are provided: skips the test with a clear message.
      A user who deliberately scoped the run to a different repo set
      (e.g., only ``--repo name=base,...`` when this test wants
      ``base + sdk``) is opting out, not misconfiguring; silent-skip
      the right behavior here.
    * If **some but not all** are provided: fails the test with a
      clear message — partial provision means the caller plausibly
      *intended* to run this test but typo'd a name or omitted one,
      and silently skipping a release-gating closure check is worse
      than failing loudly.
    """
    by_name = {r.name: r for r in all_repos}

    def _require(expected: list[str], *, kind: str | None = None) -> list[Repo]:
        present = [n for n in expected if n in by_name]
        if not present:
            pytest.skip(
                f"none of the required --repo(s) {expected} were provided; "
                f"this test is hard-coded for that named repo set. "
                f"Provided: {[(r.name, r.kind) for r in all_repos] or '<none>'}"
            )
        if len(present) != len(expected):
            missing = sorted(set(expected) - set(present))
            pytest.fail(
                f"misconfigured run: expected --repo for {expected} but "
                f"missing {missing}; this test is hard-coded for those "
                f"named repos. Pass them via --repo or use pytest -k / "
                f"--ignore to deselect this test if you intentionally "
                f"want to skip it."
            )
        repos = [by_name[n] for n in expected]
        if kind is not None:
            wrong_kind = [r for r in repos if r.kind != kind]
            if wrong_kind:
                pytest.fail(
                    f"expected all of {expected} to have kind={kind!r}, but "
                    f"{[(r.name, r.kind) for r in wrong_kind]} did not"
                )
        return repos

    return _require
