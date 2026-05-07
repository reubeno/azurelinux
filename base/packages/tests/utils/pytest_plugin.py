# SPDX-License-Identifier: MIT
"""Pytest plugin for Azure Linux RPM repo validation.

Registered via ``[project.entry-points."pytest11"]`` so that custom CLI
options are known to pytest *before* rootdir determination. This is
important here because ``--workdir`` takes a path-like value and
``--repo`` values can be long opaque strings that would otherwise risk
being interpreted as positional test-path arguments.

Responsibilities:

* Register all CLI options.
* Register the ``repo_kind`` / ``repo_name`` markers.
* Implement ``pytest_generate_tests`` to fan a test out across all
  matching ``(repo, arch)`` pairs at parametrize time, with a no-match
  guard so a typo'd marker can't silently zero out a test.

Higher-level fixtures (``repo_packages``, ``cross_repo_file_index``,
``repoclosure``, ...) live in ``conftest.py`` so tests get the
familiar pytest fixture-discovery experience.

No-``--repo`` policy
--------------------

Because this plugin is registered as a ``pytest11`` entry point, it is
loaded for **every** ``pytest`` invocation in any environment that has
``azl-repo-tests`` installed (including ``pytest --collect-only`` and
unrelated test suites that share the venv). It therefore must not fail
configuration when no ``--repo`` is provided — instead, repo-dependent
tests skip cleanly via ``pytest_generate_tests`` and the
``require_named_repos`` fixture.
"""

from __future__ import annotations

import logging

import pytest

from .repos import Repo, RepoSpecError, collect_repos

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
            "Add a repository under test (inline form). Required keys: "
            "name, kind (binary|srpm|debuginfo), url. The URL is passed "
            "through librepo, including any $basearch/$releasever "
            "placeholders. May be repeated. Combine with --repos-file "
            "and --repo-prefix as needed; at least one of --repo / "
            "--repos-file / --repo-prefix is required for any test that "
            "touches a repo."
        ),
    )
    group.addoption(
        "--repos-file",
        action="append",
        default=[],
        dest="azl_repos_files",
        metavar="PATH",
        help=(
            "Load repositories from a yum/dnf-style .repo ini file. Each "
            "section becomes one repo; the section name is the repo "
            "name, ``baseurl=`` is the URL, and a custom ``kind=`` key "
            "(binary|srpm|debuginfo) is required. May be repeated."
        ),
    )
    group.addoption(
        "--repo-prefix",
        action="append",
        default=[],
        dest="azl_repo_prefixes",
        metavar="URL",
        help=(
            "Convenience shorthand: assume URL hosts the Standard Azure "
            "Linux Repo Layout (the same layout produced by "
            "scripts/synthesize-repodata.py) and expand it into the six "
            "conventional sub-repos: base, base-debuginfo, base-srpms, "
            "sdk, sdk-debuginfo, sdk-srpms. Each is probed for "
            "repodata/repomd.xml; sub-repos that 404 are silently "
            "skipped (so a partial mirror works fine). Other HTTP/network "
            "errors are fatal. Binary/debuginfo URLs are probed using "
            "the first --arch as a sentinel and registered with a "
            "$basearch placeholder, so they still fan out across all "
            "--arch values at fetch time. Repeatable; combine with "
            "--repo / --repos-file as needed (explicit definitions "
            "override prefix-derived ones with the same name)."
        ),
    )
    group.addoption(
        "--arch",
        action="append",
        default=[],
        dest="azl_arches",
        metavar="ARCH",
        help=(
            "Architecture to test against (substituted for $basearch by "
            "librepo). May be repeated; defaults to x86_64 if not provided."
        ),
    )
    group.addoption(
        "--releasever",
        default=None,
        dest="azl_releasever",
        metavar="RELEASEVER",
        help=(
            "Release version to substitute for $releasever in URLs. "
            "Required only when at least one repo URL contains $releasever. "
            "We never inherit this from the host or from the container image."
        ),
    )
    group.addoption(
        "--workdir",
        default=None,
        dest="azl_workdir",
        metavar="DIR",
        help=(
            "Working directory for repo metadata caches. If set, it is "
            "reused as-is and never cleaned (post-mortem friendly). "
            "Otherwise a fresh temp directory is created and cleaned up "
            "at session end."
        ),
    )
    group.addoption(
        "--expected-vendor",
        default="Microsoft Corporation",
        dest="azl_expected_vendor",
        metavar="VENDOR",
        help=(
            "Expected RPM Vendor: tag for every binary package "
            "(checked by test_vendor_tag). Default: Microsoft Corporation."
        ),
    )
    group.addoption(
        "--release-suffix",
        default=r"\.azl4(~.*)?$",
        dest="azl_release_suffix",
        metavar="REGEX",
        help=(
            "Regex that every binary package's Release tag must match "
            "(checked by test_release_suffix). Default: '\\.azl4(~.*)?$' "
            "for AZL4. Override for nightly verification of older "
            "distros (e.g. AZL3) without forking the test."
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

    No-``--repo`` policy: this hook runs for **every** pytest
    invocation in the active environment (we're loaded as a
    ``pytest11`` entry point). We therefore must not raise
    :class:`pytest.UsageError` when the user provides no
    ``--repo`` — that would break ``pytest --collect-only``, IDE test
    introspection, and unrelated test suites that share the venv.
    Repo-dependent tests skip cleanly via
    :func:`pytest_generate_tests` and the ``require_named_repos``
    fixture instead.
    """
    inline = list(config.getoption("azl_repos"))
    files = list(config.getoption("azl_repos_files"))
    prefixes = list(config.getoption("azl_repo_prefixes"))

    arches: list[str] = list(config.getoption("azl_arches")) or ["x86_64"]
    seen: set[str] = set()
    deduped_arches: list[str] = []
    for a in arches:
        if a in seen:
            continue
        seen.add(a)
        deduped_arches.append(a)

    # The probing arch for --repo-prefix is the first --arch (after
    # dedup) so the user can steer the probe (e.g., --arch aarch64) when
    # x86_64 isn't published. Picking deterministically — rather than
    # probing every arch — keeps the model "one Repo per (channel, kind)
    # with $basearch placeholder" intact; asymmetric layouts should use
    # explicit --repo.
    probe_arch = deduped_arches[0]

    repos: list[Repo] = []
    if inline or files or prefixes:
        try:
            repos = collect_repos(
                inline=inline,
                file_paths=files,
                prefixes=prefixes,
                probe_arch=probe_arch,
            )
        except RepoSpecError as exc:
            raise pytest.UsageError(str(exc)) from exc

    releasever: str | None = config.getoption("azl_releasever")
    needs_releasever = any("$releasever" in r.url for r in repos)
    if needs_releasever and not releasever:
        urls_using_it = ", ".join(r.name for r in repos if "$releasever" in r.url)
        raise pytest.UsageError(
            f"--releasever is required because the URL(s) for: {urls_using_it} "
            "contain $releasever. We never inherit this from the host."
        )

    config._azl_repos = repos  # type: ignore[attr-defined]
    config._azl_arches = deduped_arches  # type: ignore[attr-defined]
    config._azl_releasever = releasever  # type: ignore[attr-defined]
    config._azl_expected_vendor = config.getoption(  # type: ignore[attr-defined]
        "azl_expected_vendor"
    )
    config._azl_release_suffix = config.getoption(  # type: ignore[attr-defined]
        "azl_release_suffix"
    )


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


def _filter_repos_by_markers(
    metafunc: pytest.Metafunc, repos: list[Repo]
) -> list[Repo]:
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
    """Fan tests out over the matching ``(repo, arch)`` pairs."""
    config = metafunc.config
    repos: list[Repo] = getattr(config, "_azl_repos", [])
    arches: list[str] = getattr(config, "_azl_arches", [])

    needs_repo = "repo" in metafunc.fixturenames
    needs_arch = "arch" in metafunc.fixturenames

    if needs_repo:
        candidates = _filter_repos_by_markers(metafunc, repos)
        if not candidates:
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
