# SPDX-License-Identifier: MIT
"""Across all provided binary repos, distinct packages from *different
SRPMs* that own the same file path must be marked as conflicting.

This is a heuristic check on top of repodata — it is not a perfect
simulation of RPM's install-time conflict resolution. It catches the
common class of "two unrelated packages own /usr/bin/foo and don't
declare Conflicts: between each other".

What gets filtered out before the check (in the metadata service):

* Directory entries — RPM permits shared directory ownership.
* ``%ghost`` entries — these mean "I claim this path but don't install
  it"; multiple packages may legitimately ``%ghost`` the same path
  (alternatives slots, log files, runtime state, etc.).
* Identical NEVRA appearing in multiple repos is deduped.

What gets filtered out *in this test* (because it's a different class
of finding):

* Same-SRPM sibling pairs. Two sub-packages produced by the same SRPM
  are checked against each other by ``rpmbuild`` at build time; if
  they reach the published repo with overlapping files, that's an
  upstream packaging hygiene issue (typically shared ``%doc``
  directories, ``%license`` files, or a missing ``%ghost`` on a
  variant tarball) rather than the install-time conflict this test is
  meant to catch. We exempt these so the cross-SRPM signal isn't
  drowned out.

What counts as "marked as conflicting" (between two packages A and B):

* A ``Conflicts:`` against the literal name of B (or vice versa), OR
* A ``Conflicts:`` against any name that B ``Provides:`` (or vice
  versa).

Both directions are checked; either side declaring the conflict is
sufficient.

Known limitations
-----------------

* **Name-only conflict matching.** ``Provides:`` and ``Conflicts:``
  are treated as bare-name sets — version ranges (``Conflicts: foo
  >= 2.0``) are not modeled. A ranged ``Conflicts:`` that does not
  match all versions of the other package will be reported as
  unsatisfied here even though the install-time resolver may accept
  it. Conversely, a same-arch pair whose ranged conflict does not
  cover the actually-published version may be reported as resolved
  here even though install would still fail. Treat this as a
  high-signal heuristic, not a perfect simulation.
* **Single-arch perspective.** The test runs once per ``(arch, repo
  set)`` pair, so we never compare an ``x86_64`` package against an
  ``aarch64`` package — that's by design (the install target is one
  arch at a time). Multilib coexistence (e.g. ``glibc.i686`` next to
  ``glibc.x86_64`` on an ``x86_64`` host) is also out of scope.
"""

from __future__ import annotations

import re

import pytest

from utils.repos import Repo
from utils.types import Package


# rules-as-code: paths where multiple owners are intentionally
# permitted (e.g. ``alternatives``-managed slots that escape the
# ghost-filter for whatever reason). Each entry's value is a short
# comment explaining why. Add new entries sparingly, and always with
# a justification.
PATH_ALLOWLIST: dict[str, str] = {
    # "/usr/bin/sendmail": "managed by alternatives across MTAs",
}


_SRPM_NAME_RE = re.compile(
    r"^(?P<name>.+)-(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?:src|nosrc)\.rpm$"
)


def _srpm_name_of(pkg: Package) -> str | None:
    """Extract the SRPM *name* from ``pkg.sourcerpm`` (e.g. ``foo``)."""
    if not pkg.sourcerpm:
        return None
    m = _SRPM_NAME_RE.match(pkg.sourcerpm)
    return m.group("name") if m else None


def _build_provides_index(
    by_repo: dict[Repo, list[Package]]
) -> dict[tuple, set[str]]:
    """Map ``(NEVRA,)`` -> the set of names the package Provides:.

    The set always includes the package's own name (which RPM
    auto-emits as a Provides:).
    """
    out: dict[tuple, set[str]] = {}
    for packages in by_repo.values():
        for pkg in packages:
            if pkg.is_source:
                continue
            out[(pkg.nevra,)] = set(pkg.provides) | {pkg.name}
    return out


def _build_conflicts_index(
    by_repo: dict[Repo, list[Package]]
) -> dict[tuple, list]:
    """Map ``(NEVRA,)`` -> the list of :class:`ConflictEntry` records."""
    out: dict[tuple, list] = {}
    for packages in by_repo.values():
        for pkg in packages:
            if pkg.is_source:
                continue
            out[(pkg.nevra,)] = list(pkg.conflicts)
    return out


def _build_srpm_index(
    by_repo: dict[Repo, list[Package]]
) -> dict[tuple, str | None]:
    """Map ``(NEVRA,)`` -> the source SRPM name (or None if unparseable)."""
    out: dict[tuple, str | None] = {}
    for packages in by_repo.values():
        for pkg in packages:
            if pkg.is_source:
                continue
            out[(pkg.nevra,)] = _srpm_name_of(pkg)
    return out


def test_file_conflicts_across_binary_repos(
    arch: str,
    binary_repos: list[Repo],
    all_binary_packages,
    cross_repo_file_index,
    subtests,
) -> None:
    if not binary_repos:
        pytest.fail(
            "misconfigured run: no binary --repo provided. This test "
            "validates a cross-repo invariant and is only meaningful "
            "with the full set of binary repos provided. Pass at least "
            "one --repo name=...,kind=binary,url=... — or use pytest -k "
            "/ --ignore to deselect this test if you intentionally want "
            "to skip it."
        )

    by_repo = all_binary_packages(arch)
    file_index = cross_repo_file_index(arch)

    provides_by_nevra = _build_provides_index(by_repo)
    conflicts_by_nevra = _build_conflicts_index(by_repo)
    srpm_by_nevra = _build_srpm_index(by_repo)

    def _are_marked_conflicting(a_nevra, b_nevra) -> bool:
        # Only **bare** (unversioned) Conflicts: entries are treated as
        # genuinely suppressing a file overlap. A versioned conflict
        # (e.g., ``Conflicts: foo < 2.0``) may not actually cover the
        # observed package version — collapsing it to a name match
        # would silently hide real install-time conflicts when the
        # versions don't satisfy the constraint. v1 of this check is
        # intentionally conservative: only the bare form suppresses;
        # versioned conflicts let the pair fall through and surface
        # for manual triage. (See "Known limitations" in the module
        # docstring.)
        a_conflicts = conflicts_by_nevra.get((a_nevra,), [])
        b_provides = provides_by_nevra.get((b_nevra,), set())
        for c in a_conflicts:
            if c.is_versioned:
                continue
            if c.name in b_provides:
                return True
        b_conflicts = conflicts_by_nevra.get((b_nevra,), [])
        a_provides = provides_by_nevra.get((a_nevra,), set())
        for c in b_conflicts:
            if c.is_versioned:
                continue
            if c.name in a_provides:
                return True
        return False

    def _same_srpm(a_nevra, b_nevra) -> bool:
        # Compare BOTH SRPM name AND arch. A noarch sub-package and an
        # arch-specific sub-package from the same SRPM can coexist on
        # one system, so a path overlap between them IS an install-time
        # conflict — even though they share an SRPM. Only true same-arch
        # siblings benefit from rpmbuild's build-time check.
        sa = srpm_by_nevra.get((a_nevra,))
        sb = srpm_by_nevra.get((b_nevra,))
        if sa is None or sa != sb:
            return False
        return a_nevra.arch == b_nevra.arch

    # (name_a, repo_a, name_b, repo_b) -> list[paths]; the tuple is
    # always sorted so order is canonical regardless of which file
    # encountered the pair first.
    pair_to_paths: dict[tuple[str, str, str, str], list[str]] = {}

    for path, owners in sorted(file_index.items()):
        if len(owners) <= 1:
            continue
        if path in PATH_ALLOWLIST:
            continue

        # Same-name owners (different NEVRAs of the same name) are not
        # an install-time conflict — only one of them ends up installed.
        names = {o.nevra.name for o in owners}
        if len(names) <= 1:
            continue

        owner_list = sorted(owners, key=lambda o: o.nevra.name)
        for i in range(len(owner_list)):
            for j in range(i + 1, len(owner_list)):
                a = owner_list[i]
                b = owner_list[j]
                if a.nevra.name == b.nevra.name:
                    continue
                if _same_srpm(a.nevra, b.nevra):
                    continue
                if _are_marked_conflicting(a.nevra, b.nevra):
                    continue
                key = tuple(sorted(
                    [(a.nevra.name, a.repo_name), (b.nevra.name, b.repo_name)]
                ))
                flat: tuple[str, str, str, str] = (
                    key[0][0], key[0][1], key[1][0], key[1][1]  # type: ignore[index]
                )
                pair_to_paths.setdefault(flat, []).append(path)

    # Each offending package pair becomes its own subtest failure.
    # Sorted worst-first so the largest offenders surface first in
    # report output. Sample paths shown per subtest.
    for (name_a, repo_a, name_b, repo_b), paths in sorted(
        pair_to_paths.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        subtest_id = f"{name_a}-vs-{name_b}"
        with subtests.test(pair=subtest_id, arch=arch):
            sample = paths[:_SAMPLE_PATHS_PER_PAIR]
            more = len(paths) - len(sample)
            lines = [
                f"on {arch}: {name_a} (from {repo_a!r}) and "
                f"{name_b} (from {repo_b!r}) own {len(paths)} shared "
                "file path(s) without a mutual Conflicts: declaration. "
                f"Sample paths:",
            ]
            for p in sample:
                lines.append(f"  {p}")
            if more > 0:
                lines.append(f"  ... and {more} more")
            pytest.fail("\n".join(lines))


# How many sample paths to show per offending package pair in each
# subtest's failure message.
_SAMPLE_PATHS_PER_PAIR = 5

