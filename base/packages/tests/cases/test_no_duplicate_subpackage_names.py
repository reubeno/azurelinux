# SPDX-License-Identifier: MIT
"""Across all binary repos, no two distinct SRPMs may produce a
sub-package of the same name.

A binary package's source SRPM is identified by RPM's ``SourceRPM``
header (which becomes ``<rpm:sourcerpm>`` in primary metadata). The
SRPM's *name* (i.e. the part before the version) is what we compare
— two binary RPMs with the same NAME built from SRPMs that share the
same SRPM name are fine; two different SRPM names producing the same
binary NAME are not.

This catches both intentional collisions (a renamed package built
twice from different sources) and accidental ones (fork-and-build
mistakes).

Rules-as-code: two policy dicts, both keyed by binary name -> the
*expected* SRPM set producing it.

* :data:`ALLOWLIST` — *intentional* multi-SRPM coexistence (compat
  shims, etc.). Silently skipped as long as the observed SRPM set is
  a subset of the listed set.
* :data:`EXPECTED_FAILURES` — *known violations we have not yet
  cleaned up*. Reported as ``XFAIL`` subtests: visible in pytest
  output and counted toward the xfail tally, but they do not fail
  the run. If a listed binary is no longer produced by multiple
  SRPMs we report a real failure (cleanup nudge — please remove
  the entry); if it picks up a *new* SRPM not in the listed set we
  also report a real failure (the ceiling has been breached).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

import pytest

from utils.repos import Repo
from utils.types import Package


# rules-as-code: package names that are *intentionally* produced by
# multiple SRPMs (e.g. compatibility shims). Each entry is a (name,
# {srpm_names...}) pair and is allowed only if the observed SRPM set
# is a subset of the listed set. Edit with a comment justifying the
# entry. Allowlisted entries are silently skipped (no XFAIL noise).
ALLOWLIST: dict[str, frozenset[str]] = {
    # "compat-foo": frozenset({"foo", "foo-compat"}),  # example
}


# Known violations we have not yet cleaned up. Each entry is the
# binary package name -> the SRPM set we expect to see producing it.
# Entries whose observed SRPM set is a subset of the listed set are
# reported as XFAIL (they show up in pytest output so they stay
# visible, but they do not fail the run). Two safety rails:
#
# * If the observed SRPM set has a *new* member not in the listed
#   set, we report a real failure — the ceiling has been breached.
# * If the binary is no longer produced by multiple SRPMs at all,
#   we report a real failure asking that the entry be removed
#   (cleanup nudge).
EXPECTED_FAILURES: dict[str, frozenset[str]] = {
    # The Ruby stdlib bundles a snapshot of "default gems" in the
    # `ruby` SRPM; several of those gems also ship as standalone
    # `rubygem-<name>` SRPMs at newer (or differently-released)
    # versions. Until we converge on a single source per gem
    # (either drop it from ruby's bundled set, or drop the
    # standalone rubygem-* SRPM), these collisions are tracked.
    "rubygem-bundler":      frozenset({"ruby", "rubygem-bundler"}),
    "rubygem-json":         frozenset({"ruby", "rubygem-json"}),
    "rubygem-minitest":     frozenset({"ruby", "rubygem-minitest"}),
    "rubygem-power_assert": frozenset({"ruby", "rubygem-power_assert"}),
    "rubygem-racc":         frozenset({"ruby", "rubygem-racc"}),
    "rubygem-rake":         frozenset({"ruby", "rubygem-rake"}),
    "rubygem-rdoc":         frozenset({"ruby", "rubygem-rdoc"}),
    "rubygem-test-unit":    frozenset({"ruby", "rubygem-test-unit"}),
}


_SRPM_RE = re.compile(r"^(?P<name>.+)-(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?:src|nosrc)\.rpm$")


def _srpm_name_of(pkg: Package) -> str | None:
    """Extract the SRPM *name* from ``pkg.sourcerpm``.

    ``sourcerpm`` is of the form ``foo-1.2-3.azl4.src.rpm``; we want
    just ``foo``.
    """
    if not pkg.sourcerpm:
        return None
    m = _SRPM_RE.match(pkg.sourcerpm)
    if m is None:
        return None
    return m.group("name")


def test_no_duplicate_subpackage_names(
    arch, all_binary_packages, binary_repos: list[Repo], subtests
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

    by_repo: dict[Repo, list[Package]] = all_binary_packages(arch)

    # binary name -> set of SRPM names contributing.
    name_to_srpms: dict[str, set[str]] = defaultdict(set)
    # binary name -> example NEVRA + repo-name pairs (one per SRPM).
    name_to_examples: dict[str, dict[str, str]] = defaultdict(dict)

    seen_nevras: set[tuple] = set()
    for repo, packages in by_repo.items():
        for pkg in packages:
            if pkg.is_source:
                continue
            # Dedupe by NEVRA across repos.
            key = (pkg.nevra,)
            if key in seen_nevras:
                continue
            seen_nevras.add(key)

            srpm_name = _srpm_name_of(pkg)
            if srpm_name is None:
                # Missing or unparseable sourcerpm: use a per-NEVRA
                # *unique* sentinel so two such packages never get
                # silently grouped into the same SRPM bucket (which
                # would let cross-package collisions slip through). The
                # sentinel deliberately includes the NEVRA so the
                # failure message points straight at the offending
                # package.
                srpm_name = f"<unparseable-sourcerpm:{pkg.nevra}>"
            name_to_srpms[pkg.name].add(srpm_name)
            name_to_examples[pkg.name].setdefault(
                srpm_name, f"{pkg.nevra} (from repo {repo.name!r})"
            )

    # Classify each multi-SRPM binary into one of:
    #   * silently allowed (matches ALLOWLIST) — skipped
    #   * expected (matches EXPECTED_FAILURES) — emit XFAIL subtest
    #   * real offender — emit FAIL subtest
    real_offenders: dict[str, set[str]] = {}
    expected_offenders: dict[str, set[str]] = {}

    for name, srpms in name_to_srpms.items():
        if len(srpms) <= 1:
            continue
        allowed = ALLOWLIST.get(name)
        if allowed is not None and srpms.issubset(allowed):
            logging.info(
                "Allowlisted multi-SRPM binary name: %s from %s",
                name, sorted(srpms),
            )
            continue
        expected = EXPECTED_FAILURES.get(name)
        if expected is not None and srpms.issubset(expected):
            expected_offenders[name] = srpms
        else:
            real_offenders[name] = srpms

    # Stale EXPECTED_FAILURES entries: listed but no longer multi-SRPM.
    # Surface them as real failures so the dict shrinks over time as
    # cleanups land.
    stale_expected: list[str] = sorted(
        name for name in EXPECTED_FAILURES
        if len(name_to_srpms.get(name, set())) <= 1
    )

    def _example_lines(name: str, srpms: set[str]) -> list[str]:
        return [
            f"  example from SRPM {srpm!r}: "
            f"{name_to_examples[name].get(srpm, '<no example>')}"
            for srpm in sorted(srpms)
        ]

    # Each offender / stale entry becomes its own subtest so it
    # appears as a distinct entry in pytest output (and in junitxml).
    # Sorted for stable reporting order.
    for name in sorted(real_offenders):
        srpms = real_offenders[name]
        with subtests.test(binary_name=name, arch=arch):
            expected = EXPECTED_FAILURES.get(name)
            if expected is not None:
                # Listed in EXPECTED_FAILURES but the observed set
                # exceeds the listed ceiling — flag the new SRPM(s).
                new = srpms - expected
                pytest.fail(
                    f"binary name {name!r} on {arch} is produced by "
                    f"{len(srpms)} distinct SRPMs: {sorted(srpms)}; "
                    f"EXPECTED_FAILURES allows {sorted(expected)} but "
                    f"observed new SRPM(s): {sorted(new)}\n"
                    + "\n".join(_example_lines(name, srpms))
                )
            pytest.fail(
                f"binary name {name!r} on {arch} is produced by "
                f"{len(srpms)} distinct SRPMs: {sorted(srpms)}\n"
                + "\n".join(_example_lines(name, srpms))
            )

    for name in sorted(expected_offenders):
        srpms = expected_offenders[name]
        with subtests.test(binary_name=name, arch=arch):
            pytest.xfail(
                f"known multi-SRPM binary (tracked in "
                f"EXPECTED_FAILURES): {name!r} on {arch} is produced "
                f"by {sorted(srpms)}\n"
                + "\n".join(_example_lines(name, srpms))
            )

    for name in stale_expected:
        with subtests.test(binary_name=name, arch=arch, kind="stale"):
            observed = sorted(name_to_srpms.get(name, set()))
            pytest.fail(
                f"{name!r} is listed in EXPECTED_FAILURES but is no "
                f"longer produced by multiple SRPMs on {arch} "
                f"(observed: {observed}). Please remove the entry."
            )
