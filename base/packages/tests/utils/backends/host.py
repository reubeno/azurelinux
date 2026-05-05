# SPDX-License-Identifier: MIT
"""Host-side dnf5 backend.

Shells out to a locally-installed ``dnf5`` to run repoclosure. All
state lives under the session workdir so a host-backend run never
modifies the user's system dnf state.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..dnf import (
    DEFAULT_REPOCLOSURE_TIMEOUT_SECS,
    build_repoclosure_argv,
    filter_repoclosure_result,
    parse_json_repoclosure_output,
    parse_text_repoclosure_output,
    probe_repoclosure_json,
    render_repo_file,
    run_command,
)
from ..repos import Repo
from ..types import RepoclosureResult

logger = logging.getLogger(__name__)


def _xdist_worker_slug() -> str:
    """Return a per-worker slug (e.g. ``gw0``) or ``"main"`` if not under xdist.

    Used to scope per-worker on-disk state so two workers running
    concurrently don't share a cachedir / repo-file directory.
    """
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


_ARCH_SETS_BY_KIND: dict[str, tuple[str, ...]] = {
    # Each entry is the set of *additional* arches (beyond the test's
    # target arch) that the dnf5 repoclosure checker should examine.
    # Note: dnf5's ``--arch`` *only* limits which packages get
    # checked; it does NOT restrict the universe of providers.
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


def _arches_to_check(check_kind: str, arch: str) -> list[str]:
    """Build the ``--arch=`` list dnf5's repoclosure plugin should use."""
    if check_kind not in _ARCH_SETS_BY_KIND:
        raise ValueError(
            f"unknown check_kind: {check_kind!r}; "
            f"expected one of {sorted(_ARCH_SETS_BY_KIND)}"
        )
    extras = _ARCH_SETS_BY_KIND[check_kind]
    if check_kind == "all":
        return []
    return [arch, *extras]


class HostBackend:
    """Run dnf5 repoclosure against the locally-installed dnf5."""

    def __init__(self, *, workdir: Path, releasever: str | None) -> None:
        self._workdir = workdir
        self._releasever = releasever
        self._json_supported: bool | None = None

    def _runner(self, argv: list[str], *, timeout: float | None = None):
        # ``timeout=None`` means "let run_command pick its default
        # probe timeout" — the long repoclosure call passes an explicit
        # value via the kwarg.
        if timeout is None:
            return run_command(argv)
        return run_command(argv, timeout=timeout)

    def _probe_json(self) -> bool:
        if self._json_supported is None:
            self._json_supported = probe_repoclosure_json(self._runner)
            logger.debug(
                "host dnf5 repoclosure --json supported: %s",
                self._json_supported,
            )
        return self._json_supported

    def _layout_for(self, universe_repos: list[Repo], arch: str) -> tuple[Path, Path]:
        """Materialize the per-run repo file and cachedir, return both paths."""
        slug = "-".join(r.name for r in universe_repos) or "empty"
        rv = self._releasever or "none"
        # Scope by xdist worker so concurrent workers don't share the
        # same cachedir / repo file (dnf5 will lock the cache and
        # serialise; worse, two writers of the same repo file race).
        worker = _xdist_worker_slug()
        layout_dir = (
            self._workdir / "host-backend" / f"rv-{rv}" / worker / arch / slug
        )
        repos_dir = layout_dir / "reposdir"
        cache_dir = layout_dir / "cache"
        repos_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        repo_file = repos_dir / "repos.repo"
        repo_file.write_text(
            render_repo_file(
                universe_repos, arch=arch, releasever=self._releasever
            )
        )
        return repo_file, cache_dir

    def repoclosure(
        self,
        *,
        target_repos: list[Repo],
        arch: str,
        universe_repos: list[Repo] | None = None,
        check_kind: str = "binary",
    ) -> RepoclosureResult:
        if not target_repos:
            raise ValueError("target_repos must be non-empty")
        if universe_repos is None:
            universe_repos = target_repos
        universe_names = {r.name for r in universe_repos}
        missing = [r.name for r in target_repos if r.name not in universe_names]
        if missing:
            raise ValueError(
                f"target_repos {missing} are not in universe_repos "
                f"{sorted(universe_names)}"
            )

        repo_file, cache_dir = self._layout_for(universe_repos, arch)
        use_json = self._probe_json()
        argv = build_repoclosure_argv(
            repo_file_path=str(repo_file),
            universe_repos=universe_repos,
            arches_to_check=_arches_to_check(check_kind, arch),
            releasever=self._releasever,
            use_json=use_json,
            cachedir=str(cache_dir),
        )
        result = self._runner(argv, timeout=DEFAULT_REPOCLOSURE_TIMEOUT_SECS)
        target_names = tuple(r.name for r in target_repos)
        if use_json:
            outcome = parse_json_repoclosure_output(
                stdout=result.stdout,
                target_repo_names=target_names,
                arch=arch,
            )
        else:
            outcome = parse_text_repoclosure_output(
                stdout=result.stdout,
                target_repo_names=target_names,
                arch=arch,
            )

        # Distinguish "unresolved deps" (parsed) from a hard error
        # (e.g., metadata fetch failure). If rc!=0 AND we parsed
        # nothing, treat the run itself as failed.
        if result.returncode != 0 and not outcome.unresolved:
            tail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"dnf5 repoclosure failed (rc={result.returncode}) "
                f"for {target_names} on {arch}; stderr/stdout tail:\n{tail[-2000:]}"
            )

        arch_set = (
            None
            if check_kind == "all"
            else set(_arches_to_check(check_kind, arch))
        )
        # For "buildtime", the *point* of the check is to surface BOTH
        # unresolved BuildRequires of source packages AND broken runtime
        # closure of the binary providers that satisfy them — so we
        # must NOT drop findings whose source repo is outside
        # ``target_repos``. Filtering by target repo would silently
        # erase exactly the binary-provider failures the kind exists
        # to catch (and would do so only on dnf5 builds with --json
        # support, since text output has no per-finding repo, making
        # the bug a host-version-dependent split-brain).
        target_filter = (
            None
            if check_kind == "buildtime"
            else {r.name for r in target_repos}
        )
        return filter_repoclosure_result(
            outcome,
            target_repo_names=target_filter,
            arches_to_keep=arch_set,
            raw_target_repo_names=target_names,
        )
