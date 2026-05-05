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
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from utils.repos import Repo
from utils.types import Package


# rules-as-code: package names that are *intentionally* produced by
# multiple SRPMs (e.g. compatibility shims). Each entry is a (name,
# {srpm_names...}) pair and is allowed only if the observed SRPM set
# is a subset of the listed set. Edit with a comment justifying the
# entry.
ALLOWLIST: dict[str, frozenset[str]] = {
    # "compat-foo": frozenset({"foo", "foo-compat"}),  # example
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
        import pytest
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

    offenders: dict[str, set[str]] = {}
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
        offenders[name] = srpms

    # Each offender becomes its own subtest failure so it appears as a
    # distinct entry in pytest output (and in junitxml etc.). The
    # test function itself remains a single collected case.
    for name, srpms in sorted(offenders.items()):
        with subtests.test(binary_name=name, arch=arch):
            example_lines = [
                f"  example from SRPM {srpm!r}: "
                f"{name_to_examples[name].get(srpm, '<no example>')}"
                for srpm in sorted(srpms)
            ]
            import pytest
            pytest.fail(
                f"binary name {name!r} on {arch} is produced by "
                f"{len(srpms)} distinct SRPMs: {sorted(srpms)}\n"
                + "\n".join(example_lines)
            )
