# SPDX-License-Identifier: MIT
"""Container-based dnf5 backend.

Runs the same dnf5 invocations as :class:`HostBackend`, but inside a
container (default ``fedora:44``) so the test run is decoupled from
the host's dnf5 version. Useful when the host's dnf5 is too old to
support ``dnf5 repoclosure --json`` (or the latest repoclosure
features more generally).

State (rendered .repo file + cache) lives under
``<workdir>/container-backend/...`` — separate from the host
backend's state to keep caches scoped per backend (a single workdir
can host both backends without contamination).
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from ..dnf import (
    DEFAULT_PROBE_TIMEOUT_SECS,
    DEFAULT_REPOCLOSURE_TIMEOUT_SECS,
    build_repoclosure_argv,
    filter_repoclosure_result,
    parse_json_repoclosure_output,
    parse_text_repoclosure_output,
    probe_repoclosure_json,
    render_repo_file,
)
from ..repos import Repo
from ..types import RepoclosureResult
from .host import _arches_to_check, _xdist_worker_slug  # shared helpers

logger = logging.getLogger(__name__)


# Where the host workdir is bind-mounted inside the container.
_CONTAINER_WORKDIR = "/azl-repo-tests"


class ContainerBackend:
    """Run dnf5 repoclosure inside a configurable container."""

    def __init__(
        self,
        *,
        workdir: Path,
        releasever: str | None,
        image: str,
        runtime: str,
    ) -> None:
        self._workdir = workdir
        self._releasever = releasever
        self._image = image
        self._runtime = runtime
        self._json_supported: bool | None = None
        self._dnf5_installed: bool = False
        self._selinux = Path("/sys/fs/selinux").exists()

    # ------------------------------------------------------------------
    # Container-state setup
    # ------------------------------------------------------------------

    def _container_subdir(self) -> Path:
        # Scope by xdist worker so concurrent workers don't share a
        # bind-mounted dir. The first ``:Z`` relabel of a directory
        # (under SELinux) is what makes the second worker observe the
        # mount as foreign-labelled — splitting the dir per-worker
        # sidesteps that entirely. Even without SELinux, two workers
        # sharing the same cachedir is a recipe for dnf5 lock
        # contention and partial-state corruption.
        worker = _xdist_worker_slug()
        sub = self._workdir / "container-backend" / worker
        sub.mkdir(parents=True, exist_ok=True)
        return sub

    def _bind_mount_args(self) -> list[str]:
        sub = self._container_subdir()
        opt = f"{sub}:{_CONTAINER_WORKDIR}"
        if self._selinux:
            opt += ":Z"
        return ["-v", opt]

    def _proxy_env_args(self) -> list[str]:
        """Forward proxy/CA env vars into the container if set on the host.

        We deliberately use the bare ``-e KEY`` form (no ``=value``)
        so the runtime *inherits* each variable from this process's
        environment instead of inlining the value into argv. The argv
        gets logged (debug) and embedded in the ``RuntimeError`` raised
        on timeout; with values like ``HTTPS_PROXY=http://user:pass@…``
        that would leak credentials into captured logs and CI artifacts.
        Passing only the key keeps the secret out of argv entirely.
        """
        forwarded: list[str] = []
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
        ):
            if key in os.environ:
                forwarded.extend(["-e", key])
        return forwarded

    def _ensure_dnf5(self) -> None:
        """Make sure dnf5 is available in the configured image.

        Fedora 44+ ships dnf5 by default, but we run a one-time check
        so we fail with a clear message on older images.
        """
        if self._dnf5_installed:
            return
        # ``dnf5 --version`` prints to stdout on success.
        argv = [
            self._runtime, "run", "--rm",
            *self._bind_mount_args(),
            self._image,
            "dnf5", "--version",
        ]
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_PROBE_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"container image {self._image!r} does not have dnf5 "
                f"available; rc={result.returncode}, stderr:\n"
                f"{(result.stderr or '').strip()[-1000:]}"
            )
        logger.debug(
            "container dnf5 available in %s: %s",
            self._image, result.stdout.strip(),
        )
        self._dnf5_installed = True

    # ------------------------------------------------------------------
    # In-container command runner
    # ------------------------------------------------------------------

    def _runner(self, argv: list[str], *, timeout: float | None = None):
        self._ensure_dnf5()
        full = [
            self._runtime, "run", "--rm",
            *self._bind_mount_args(),
            *self._proxy_env_args(),
            self._image,
            *argv,
        ]
        # ``timeout=None`` means "use the probe-length default"; the
        # repoclosure call passes an explicit (longer) value.
        effective_timeout = (
            timeout if timeout is not None else DEFAULT_PROBE_TIMEOUT_SECS
        )
        logger.debug(
            "Running in container (timeout=%s): %s",
            effective_timeout, " ".join(full),
        )
        try:
            return subprocess.run(
                full,
                check=False,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"container command timed out after {effective_timeout}s: "
                f"{' '.join(full)}"
            ) from exc

    def _probe_json(self) -> bool:
        if self._json_supported is None:
            self._json_supported = probe_repoclosure_json(self._runner)
            logger.debug(
                "container dnf5 repoclosure --json supported: %s",
                self._json_supported,
            )
        return self._json_supported

    # ------------------------------------------------------------------
    # repoclosure
    # ------------------------------------------------------------------

    def _layout_for(
        self, universe_repos: list[Repo], arch: str
    ) -> tuple[str, str]:
        """Render the .repo file under workdir; return its container path + cachedir.

        The .repo file contains every repo in *universe_repos* (the
        universe dnf5 sees inside the container).
        """
        slug = "-".join(r.name for r in universe_repos) or "empty"
        rv = self._releasever or "none"
        rel = Path("layouts") / f"rv-{rv}" / arch / slug
        host_dir = self._container_subdir() / rel
        repos_dir = host_dir / "reposdir"
        cache_dir = host_dir / "cache"
        repos_dir.mkdir(parents=True, exist_ok=True)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (repos_dir / "repos.repo").write_text(
            render_repo_file(
                universe_repos, arch=arch, releasever=self._releasever
            )
        )
        rel_repo_file = rel / "reposdir" / "repos.repo"
        rel_cache = rel / "cache"
        return (
            f"{_CONTAINER_WORKDIR}/{rel_repo_file.as_posix()}",
            f"{_CONTAINER_WORKDIR}/{rel_cache.as_posix()}",
        )

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
            repo_file_path=repo_file,
            universe_repos=universe_repos,
            arches_to_check=_arches_to_check(check_kind, arch),
            releasever=self._releasever,
            use_json=use_json,
            cachedir=cache_dir,
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
        if result.returncode != 0 and not outcome.unresolved:
            tail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"container dnf5 repoclosure failed (rc={result.returncode}) "
                f"for {target_names} on {arch}; stderr/stdout tail:\n{tail[-2000:]}"
            )
        arch_set = (
            None
            if check_kind == "all"
            else set(_arches_to_check(check_kind, arch))
        )
        # See HostBackend.repoclosure for why "buildtime" disables the
        # per-target-repo filter — this is the same fix in both
        # backends to keep their semantics aligned.
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


__all__ = ["ContainerBackend"]
