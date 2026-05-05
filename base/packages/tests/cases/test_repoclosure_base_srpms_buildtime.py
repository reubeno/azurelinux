# SPDX-License-Identifier: MIT
"""Every SRPM in the ``base-srpms`` repo must be build-time-closed
against ``base + sdk`` binary repos.

This asserts that for each source RPM in ``base-srpms``, its
``BuildRequires:`` set is satisfiable by binary providers in
``base ∪ sdk ∪ base-srpms``.

How this maps to dnf5
---------------------

dnf5 represents an SRPM's ``BuildRequires:`` as ``Requires:`` on the
source-arch package in the SRPM repo's ``primary.xml``. So
``dnf5 repoclosure`` over an SRPM repo, with the binary universe
enabled, naturally checks build-time closure.

Why ``check_kind="buildtime"`` (not "source-only")
--------------------------------------------------

Filtering the checker to source-arch only would silently miss a
critical transitive failure: if an SRPM's BuildRequires *name*
resolves to a binary provider, but that binary provider's own
runtime deps are unsatisfied, the SRPM is still not buildable in
practice. ``buildtime`` includes ``arch``, ``noarch``, ``src``, and
``nosrc`` so both the SRPM-level BuildRequires *and* the runtime
closure of any binary that participates as a provider are checked.

Skip / fail behavior
--------------------

Hard-coded for ``base-srpms`` + ``base`` + ``sdk``. If **none** of
those ``--repo`` flags are provided, the test skips (a user who scoped
the run to a different repo set is opting out, not misconfiguring).
If **some but not all** are provided, the test fails — partial
provision is almost certainly a typo or omission rather than a
deliberate opt-out, and silently skipping a release-gating closure
check is worse than failing loudly.
"""

from __future__ import annotations


def test_repoclosure_base_srpms_buildtime(
    arch: str, require_named_repos, repoclosure, subtests
) -> None:
    srpms = require_named_repos(["base-srpms"], kind="srpm")
    binaries = require_named_repos(["base", "sdk"], kind="binary")
    result = repoclosure(
        target_repos=srpms,
        arch=arch,
        universe_repos=srpms + binaries,
        check_kind="buildtime",
    )
    if result.success:
        return
    for nevra, missing in sorted(result.unresolved.items(), key=lambda kv: str(kv[0])):
        repo = result.repos_by_nevra.get(nevra)
        suffix = f" (from {repo!r})" if repo else ""
        with subtests.test(package=str(nevra), arch=arch):
            import pytest
            pytest.fail(
                f"{nevra}{suffix} has unresolved dep(s):\n"
                + "\n".join(f"  - {d}" for d in missing)
            )
