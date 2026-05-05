# SPDX-License-Identifier: MIT
"""``base`` and ``sdk`` together must be closed over runtime dependencies.

This test is hard-coded for the (``base`` + ``sdk``) combination. It
runs once per architecture.

Behavior:

* If both ``--repo name=base,...`` and ``--repo name=sdk,...`` were
  provided, run repoclosure with the union as the universe.
* If either is missing, fail loudly. Hard-coded closure tests are
  only meaningful with the full set of named repos provided;
  silently skipping a release-gating check is worse than failing.
  Use ``pytest -k`` / ``--ignore`` to deselect intentionally.
"""

from __future__ import annotations


def test_repoclosure_base_plus_sdk(
    arch: str, require_named_repos, repoclosure, subtests
) -> None:
    repos = require_named_repos(["base", "sdk"], kind="binary")
    result = repoclosure(repos, arch)
    if result.success:
        return
    for nevra, missing in sorted(result.unresolved.items(), key=lambda kv: str(kv[0])):
        repo = result.repos_by_nevra.get(nevra)
        suffix = f" (from {repo!r})" if repo else ""
        with subtests.test(package=str(nevra), arch=arch):
            import pytest
            pytest.fail(
                f"{nevra}{suffix} has unresolved runtime dep(s):\n"
                + "\n".join(f"  - {d}" for d in missing)
            )
