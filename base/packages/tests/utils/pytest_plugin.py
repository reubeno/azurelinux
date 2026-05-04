# SPDX-License-Identifier: MIT
"""Pytest plugin for Azure Linux RPM repo validation.

Registered via ``[project.entry-points."pytest11"]`` so that custom CLI
options are known to pytest *before* rootdir determination. This is
important here because ``--workdir`` takes a path-like value and
``--repo`` values can be long opaque strings that would otherwise risk
being interpreted as positional test-path arguments.

Responsibilities:

* Register all CLI options.
* Register the ``repo_kind`` / ``repo_name`` / ``allow_no_repos`` markers.
* Implement ``pytest_generate_tests`` to fan a test out across all
  matching ``(repo, arch)`` pairs at parametrize time, with a no-match
  guard so a typo'd marker can't silently zero out a test.
* Provide a small set of helpers used by ``conftest.py``.

The plugin module exposes only pytest hooks and tiny pure helpers.
Higher-level fixtures (``repo_packages``, ``cross_repo_file_index``,
``repoclosure``, ...) live in ``conftest.py`` so tests get the
familiar pytest fixture-discovery experience.
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

import pytest

from .repos import Repo, RepoSpecError, parse_repo_specs

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI options
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register all CLI options for the repo-validation suite."""

    group = parser.getgroup("azl-repo", "Azure Linux RPM repo validation")
    group.addoption(
        "--repo",
        action="append",
        default=[],
        dest="azl_repos",
        metavar="name=...,kind=...,url=...",
        help=(
            "Add a repository under test. Required keys: name, kind "
            "(binary|srpm|debuginfo), url. The URL is passed verbatim to "
            "dnf, including any $basearch/$releasever placeholders. "
            "Repeat for each repo. At least one --repo is required."
        ),
    )
    group.addoption(
        "--arch",
        action="append",
        default=[],
        dest="azl_arches",
        metavar="ARCH",
        help=(
            "Architecture to test against (substituted for $basearch by dnf). "
            "May be repeated; defaults to x86_64 if not provided."
        ),
    )
    group.addoption(
        "--releasever",
        default=None,
        dest="azl_releasever",
        metavar="RELEASEVER",
        help=(
            "Release version to substitute for $releasever in URLs. "
            "Required only when at least one --repo URL contains $releasever. "
            "We never inherit this from the host or from the container image."
        ),
    )
    group.addoption(
        "--repoclosure-backend",
        choices=("host", "container"),
        default="host",
        dest="azl_repoclosure_backend",
        help="How to invoke dnf5 for repoclosure. Default: host.",
    )
    group.addoption(
        "--container-image",
        default="fedora:44",
        dest="azl_container_image",
        metavar="IMAGE",
        help="Container image used by --repoclosure-backend container.",
    )
    group.addoption(
        "--container-runtime",
        default=None,
        dest="azl_container_runtime",
        metavar="RUNTIME",
        help=(
            "Container runtime to use (podman or docker). "
            "If unset, podman is preferred when available."
        ),
    )
    group.addoption(
        "--workdir",
        default=None,
        dest="azl_workdir",
        metavar="DIR",
        help=(
            "Working directory for repo metadata caches and dnf state. "
            "If set, it is reused as-is and never cleaned (post-mortem "
            "friendly). Otherwise a fresh temp directory is created and "
            "cleaned up at session end."
        ),
    )


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Validate global CLI args early and record session-wide derived state.

    We do parsing/validation here (rather than in fixtures) so a
    misconfiguration is reported once with a clean message, before any
    test collection or fixture setup.
    """
    raw_repos: list[str] = list(config.getoption("azl_repos"))
    if not raw_repos:
        raise pytest.UsageError(
            "at least one --repo name=...,kind=...,url=... is required"
        )

    try:
        repos = parse_repo_specs(raw_repos)
    except RepoSpecError as exc:
        raise pytest.UsageError(str(exc)) from exc

    arches: list[str] = list(config.getoption("azl_arches")) or ["x86_64"]
    # Preserve order while de-duping.
    seen: set[str] = set()
    deduped_arches: list[str] = []
    for a in arches:
        if a in seen:
            continue
        seen.add(a)
        deduped_arches.append(a)

    releasever: str | None = config.getoption("azl_releasever")
    needs_releasever = any("$releasever" in r.url for r in repos)
    if needs_releasever and not releasever:
        urls_using_it = ", ".join(r.name for r in repos if "$releasever" in r.url)
        raise pytest.UsageError(
            f"--releasever is required because the URL(s) for: {urls_using_it} "
            "contain $releasever. We never inherit this from the host."
        )

    backend = config.getoption("azl_repoclosure_backend")
    if backend == "container":
        runtime = config.getoption("azl_container_runtime") or _detect_container_runtime()
        if runtime is None:
            raise pytest.UsageError(
                "--repoclosure-backend container requires podman or docker on PATH "
                "(or pass --container-runtime explicitly)."
            )
        config._azl_container_runtime = runtime  # type: ignore[attr-defined]

    config._azl_repos = repos  # type: ignore[attr-defined]
    config._azl_arches = deduped_arches  # type: ignore[attr-defined]
    config._azl_releasever = releasever  # type: ignore[attr-defined]


def _detect_container_runtime() -> str | None:
    """Return ``podman`` if available, else ``docker``, else ``None``."""
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Marker-driven parametrization
# ---------------------------------------------------------------------------


def _get_marker_values(metafunc: pytest.Metafunc, name: str) -> list[str]:
    """Collect arg values from all markers of the given name on a test."""
    values: list[str] = []
    for marker in metafunc.definition.iter_markers(name):
        if not marker.args:
            raise pytest.UsageError(
                f"@pytest.mark.{name}(...) requires an argument on "
                f"{metafunc.definition.nodeid}"
            )
        values.extend(str(a) for a in marker.args)
    return values


def _filter_repos_by_markers(metafunc: pytest.Metafunc, repos: list[Repo]) -> list[Repo]:
    """Apply ``repo_kind`` / ``repo_name`` markers to narrow the repo set."""
    kinds = _get_marker_values(metafunc, "repo_kind")
    names = _get_marker_values(metafunc, "repo_name")

    filtered = repos
    if kinds:
        kinds_set = set(kinds)
        filtered = [r for r in filtered if r.kind in kinds_set]
    if names:
        names_set = set(names)
        filtered = [r for r in filtered if r.name in names_set]
    return filtered


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Fan tests out over the matching ``(repo, arch)`` pairs.

    Rules:

    * If the test has a ``repo`` parameter, parametrize over every
      provided repo that matches the test's ``repo_kind`` /
      ``repo_name`` markers. Each entry pairs with the configured
      arches via the ``arch`` parameter (also parametrized).
    * If the test only has an ``arch`` parameter (no ``repo``),
      parametrize over arches alone.
    * If marker filters eliminate every candidate repo for a test that
      requires one, raise :class:`pytest.UsageError` so the test
      doesn't silently disappear. The ``allow_no_repos`` marker opts
      out of this guard.
    """
    config = metafunc.config
    repos: list[Repo] = getattr(config, "_azl_repos", [])
    arches: list[str] = getattr(config, "_azl_arches", [])

    needs_repo = "repo" in metafunc.fixturenames
    needs_arch = "arch" in metafunc.fixturenames

    if needs_repo:
        candidates = _filter_repos_by_markers(metafunc, repos)
        if not candidates:
            # No matching --repo for this test's markers. Skip cleanly
            # rather than raising a collection error: a user may
            # legitimately want to run only a subset of tests for the
            # repo kinds they provided. The skip message is specific
            # enough to make the cause obvious.
            kinds = _get_marker_values(metafunc, "repo_kind")
            names = _get_marker_values(metafunc, "repo_name")
            reason = (
                f"no --repo matched markers (kinds={kinds or '<any>'}, "
                f"names={names or '<any>'}); "
                f"provided: {[(r.name, r.kind) for r in repos]}"
            )
            metafunc.parametrize(
                "repo",
                [pytest.param(None, marks=pytest.mark.skip(reason=reason))],
                ids=["no-matching-repo"],
            )
            if needs_arch:
                metafunc.parametrize("arch", arches or ["x86_64"])
            return

        if needs_arch:
            params = [(r, a) for r in candidates for a in arches]
            ids = [f"{r.name}-{a}" for r, a in params]
            metafunc.parametrize("repo,arch", params, ids=ids)
        else:
            metafunc.parametrize(
                "repo", candidates, ids=[r.name for r in candidates]
            )
        return

    if needs_arch:
        metafunc.parametrize("arch", arches)
