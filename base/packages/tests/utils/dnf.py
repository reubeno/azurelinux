# SPDX-License-Identifier: MIT
"""Shared helpers for invoking ``dnf5``.

Both the host and container backends use these to construct their
arguments consistently.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .repos import Repo
from .types import NEVRA, RepoclosureResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Subprocess timeouts
# ---------------------------------------------------------------------------

# Short-running probes: ``dnf5 --version``, ``dnf5 repoclosure --help``.
DEFAULT_PROBE_TIMEOUT_SECS = 30

# repoclosure itself: dependency resolution over a large universe can
# legitimately take several minutes the first time (network fetch +
# SAT solving). 600s gives that headroom while still bounding hangs
# from a stuck mirror or a deadlocked dnf5.
DEFAULT_REPOCLOSURE_TIMEOUT_SECS = 600


# ---------------------------------------------------------------------------
# .repo file generation
# ---------------------------------------------------------------------------


def render_repo_file(repos: list[Repo]) -> str:
    """Render an ini-style .repo file body for *repos*.

    GPG checks are disabled — we're validating already-published
    metadata structure, not establishing trust. The user's published
    repos may or may not be signed; signature validation is out of
    scope for these tests.
    """
    lines: list[str] = []
    for r in repos:
        lines.append(f"[{r.name}]")
        lines.append(f"name={r.name}")
        lines.append(f"baseurl={r.url}")
        lines.append("enabled=1")
        lines.append("gpgcheck=0")
        lines.append("repo_gpgcheck=0")
        lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


@dataclass
class DnfInvocation:
    """A constructed dnf5 invocation, ready for ``subprocess.run``."""

    argv: list[str]
    repo_file_contents: str
    """Contents of the .repo file the caller is responsible for writing."""


def build_repoclosure_argv(
    *,
    repo_file_path: str,
    universe_repos: list[Repo],
    arches_to_check: list[str],
    releasever: str | None,
    use_json: bool,
    cachedir: str,
) -> list[str]:
    """Build the argv for ``dnf5 repoclosure ...``.

    *repo_file_path* is the path (in the relevant filesystem — host or
    container) of the rendered .repo file. The file should contain one
    section per repo in *universe_repos* — that's the *universe* of
    packages dnf5 considers when resolving dependencies.

    *arches_to_check* drives ``--arch=<...>`` flags. dnf5's
    ``repoclosure --arch=<ARCH>`` option means "only **check**
    packages of these arches" — it does NOT restrict the dependency
    universe (binary providers of any arch are still consulted to
    satisfy a checked package's deps). For runtime checks pass
    ``[<target-arch>, "noarch"]``; for build-time SRPM checks, also
    include ``"src"`` and ``"nosrc"``.

    *cachedir* is a path in the relevant filesystem.

    We deliberately do NOT pass ``--check=<repos>``: that flag has the
    side effect of dropping source-arch packages from the checker even
    when their repo is named, so it silently hides build-time closure
    findings. Filtering by repo (when needed) is done in the
    :class:`RepoclosureResult` consumer instead.
    """
    argv: list[str] = ["dnf5"]
    repos_dir = str(Path(repo_file_path).parent)
    argv.extend([
        "--quiet",
        "--setopt", f"reposdir={repos_dir}",
        "--setopt", f"cachedir={cachedir}",
        "--setopt", "gpgcheck=0",
        "--setopt", "repo_gpgcheck=0",
    ])
    if releasever:
        argv.extend(["--setopt", f"releasever={releasever}"])
    argv.append("repoclosure")
    for a in arches_to_check:
        argv.append(f"--arch={a}")
    if use_json:
        argv.append("--json")
    return argv


# ---------------------------------------------------------------------------
# Probing dnf5 for --json support
# ---------------------------------------------------------------------------


def probe_repoclosure_json(run: callable) -> bool:
    """Return True iff ``dnf5 repoclosure --help`` advertises ``--json``.

    *run* takes a list of args and returns a CompletedProcess-ish
    object with ``.stdout`` (str). Both backends inject their own
    runner so we can probe inside a container if needed.
    """
    try:
        result = run(["dnf5", "repoclosure", "--help"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("dnf5 repoclosure --help failed: %s", exc)
        return False
    text = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    return "--json" in text


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------


def parse_json_repoclosure_output(
    *, stdout: str, target_repo_names: tuple[str, ...], arch: str
) -> RepoclosureResult:
    """Parse the JSON output of ``dnf5 repoclosure --json``.

    The structure produced by dnf5 (verified against fedora:44's
    dnf5 5.4.x) is a top-level array of objects, each with::

        {
          "package": "<NEVRA-string>",
          "repo": "<repo-name>",
          "unresolved_dependencies": ["dep1", "dep2", ...]
        }

    We're tolerant of variations:

    * Object-wrapped form (``{"problems": [...]}``) is accepted.
    * Alternate key names (``missing`` / ``unresolved`` / ``requires``)
      are accepted as fallback for the dependency list.
    * Missing or malformed entries are silently dropped (we err on the
      side of permissive parsing — a hard error in our parser would be
      worse than missing one finding).

    Pre-JSON noise (warnings, notices) printed by dnf5 before the
    actual JSON document is tolerated by skipping ahead to the first
    ``[`` or ``{`` before attempting to decode.
    """
    raw = stdout.strip()
    if not raw:
        return RepoclosureResult(
            target_repo_names=target_repo_names, arch=arch, raw_output=stdout
        )

    payload = _slice_to_json_start(raw)
    if payload is None:
        # No JSON-looking content at all — fall back to text parser.
        return parse_text_repoclosure_output(
            stdout=stdout, target_repo_names=target_repo_names, arch=arch
        )

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        # Mixed text/JSON output — fall back to text parser.
        return parse_text_repoclosure_output(
            stdout=stdout, target_repo_names=target_repo_names, arch=arch
        )

    entries: list[dict] = []
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        for key in ("problems", "unresolved", "packages"):
            if isinstance(data.get(key), list):
                entries = [e for e in data[key] if isinstance(e, dict)]
                break

    unresolved: dict[NEVRA, list[str]] = {}
    repos_by_nevra: dict[NEVRA, str] = {}
    for entry in entries:
        pkg = entry.get("package") or entry.get("nevra") or entry.get("nvra")
        nevra = _parse_nevra_string(pkg) if isinstance(pkg, str) else None
        if nevra is None:
            continue
        missing_raw = (
            entry.get("unresolved_dependencies")
            or entry.get("missing")
            or entry.get("unresolved")
            or entry.get("requires")
            or []
        )
        missing = [str(m) for m in missing_raw if m]
        unresolved.setdefault(nevra, []).extend(missing)
        repo = entry.get("repo")
        if isinstance(repo, str) and repo:
            repos_by_nevra[nevra] = repo

    return RepoclosureResult(
        target_repo_names=target_repo_names,
        arch=arch,
        unresolved=unresolved,
        raw_output=stdout,
        repos_by_nevra=repos_by_nevra,
    )


# Match lines like:
#   package: foo-1.2-3.azl4.x86_64 (from <repoid>)
# or
#     unresolved deps:
#       libbar.so.1()(64bit)
_TEXT_PKG_LINE = re.compile(r"^\s*package:\s*(\S+)")
_TEXT_DEP_LINE = re.compile(r"^\s+(?!unresolved)(\S.*)$")


def filter_repoclosure_result(
    result: RepoclosureResult,
    *,
    target_repo_names: set[str] | None,
    arches_to_keep: set[str] | None,
    raw_target_repo_names: tuple[str, ...] | None = None,
) -> RepoclosureResult:
    """Return a copy of *result* keeping only findings the test cares about.

    * *target_repo_names* — if not ``None``, drop findings for packages
      not from these repos. Per-repo attribution is only available
      with the JSON parser; when a finding has no recorded repo, we
      keep it (better to over-report than silently drop).
    * *arches_to_keep* — if not ``None``, drop findings whose package
      NEVRA arch is not in this set.
    """
    raw_target = raw_target_repo_names or result.target_repo_names
    filtered: dict[NEVRA, list[str]] = {}
    repos_filtered: dict[NEVRA, str] = {}
    for nevra, missing in result.unresolved.items():
        if arches_to_keep is not None and nevra.arch not in arches_to_keep:
            continue
        if target_repo_names is not None:
            repo = result.repos_by_nevra.get(nevra)
            if repo is not None and repo not in target_repo_names:
                continue
        filtered[nevra] = missing
        if nevra in result.repos_by_nevra:
            repos_filtered[nevra] = result.repos_by_nevra[nevra]
    return RepoclosureResult(
        target_repo_names=raw_target,
        arch=result.arch,
        unresolved=filtered,
        raw_output=result.raw_output,
        repos_by_nevra=repos_filtered,
    )


def _slice_to_json_start(s: str) -> str | None:
    """Return *s* sliced to the first ``[`` or ``{``, or ``None``.

    Some dnf5 builds emit warnings/notices on stdout *before* the JSON
    document (e.g. cache miss notices, locale warnings). Strip that
    prelude so ``json.loads`` sees a clean payload.
    """
    candidates = []
    for ch in ("[", "{"):
        idx = s.find(ch)
        if idx != -1:
            candidates.append(idx)
    if not candidates:
        return None
    return s[min(candidates):]


def parse_text_repoclosure_output(
    *, stdout: str, target_repo_names: tuple[str, ...], arch: str
) -> RepoclosureResult:
    """Best-effort parser for the line-oriented dnf5 repoclosure output.

    The exact format varies between dnf5 versions; we tolerate that by
    extracting any package-like header lines and any indented
    dependency-like lines that follow them.
    """
    unresolved: dict[NEVRA, list[str]] = {}
    current: NEVRA | None = None
    for raw_line in stdout.splitlines():
        m_pkg = _TEXT_PKG_LINE.match(raw_line)
        if m_pkg:
            current = _parse_nevra_string(m_pkg.group(1))
            if current is not None:
                unresolved.setdefault(current, [])
            continue
        if current is None:
            continue
        line = raw_line.strip()
        if not line:
            current = None
            continue
        # Skip section headers like "unresolved deps:".
        if line.endswith(":"):
            continue
        unresolved[current].append(line)
    # Drop empty-valued packages — they were headers without missing-dep lines.
    unresolved = {k: v for k, v in unresolved.items() if v}
    # If dnf5 produced non-empty output but we extracted zero findings,
    # the output format has likely changed in a way our heuristics
    # don't recognize. Surface that as a WARNING (not a hard error —
    # an empty-but-non-blank output is also possible from purely
    # informational text).
    if not unresolved and stdout.strip():
        logger.warning(
            "parse_text_repoclosure_output: dnf5 produced %d bytes of "
            "output but no findings were extracted. The output format "
            "may have changed. First 500 chars of output:\n%s",
            len(stdout), stdout[:500],
        )
    return RepoclosureResult(
        target_repo_names=target_repo_names,
        arch=arch,
        unresolved=unresolved,
        raw_output=stdout,
    )


_NEVRA_RE = re.compile(
    r"^(?P<name>.+)-(?P<ver>[^-]+)-(?P<rel>[^-]+)\.(?P<arch>[^.]+)$"
)


def _parse_nevra_string(s: str) -> NEVRA | None:
    """Parse a ``name-ver-rel.arch`` string. Epoch is assumed 0 if absent."""
    s = s.strip().rstrip(",")
    epoch = 0
    if ":" in s:
        ep, _, rest = s.partition(":")
        # epoch may appear as ``name-EPOCH:ver-rel.arch`` or as a
        # bare leading ``EPOCH:`` — handle the latter, leave the
        # former (which is unusual in dnf output) alone.
        try:
            epoch = int(ep)
            s = rest
        except ValueError:
            pass
    m = _NEVRA_RE.match(s)
    if m is None:
        return None
    return NEVRA(
        name=m.group("name"),
        epoch=epoch,
        version=m.group("ver"),
        release=m.group("rel"),
        arch=m.group("arch"),
    )


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def run_command(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = DEFAULT_PROBE_TIMEOUT_SECS,
) -> subprocess.CompletedProcess:
    """Run *argv* and return the CompletedProcess. stdout/stderr captured.

    Does not raise on non-zero exit — callers (the backends and the
    JSON probe) interpret exit codes themselves, since
    ``repoclosure`` returns 1 for "unresolved deps" which is not a
    hard error from our perspective.

    *timeout* is enforced via ``subprocess.run(timeout=...)``; on
    expiry we re-raise as a ``RuntimeError`` with a descriptive
    message rather than letting the raw ``TimeoutExpired`` propagate
    (the latter loses argv context once it bubbles up through pytest).
    Pass ``timeout=None`` to disable (callers should not — pick an
    appropriate constant from this module).
    """
    logger.debug("Running (timeout=%s): %s", timeout, " ".join(argv))
    started = time.monotonic()
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        partial_stdout = (exc.stdout or b"")[:500] if isinstance(exc.stdout, bytes) else (exc.stdout or "")[:500]
        partial_stderr = (exc.stderr or b"")[:500] if isinstance(exc.stderr, bytes) else (exc.stderr or "")[:500]
        raise RuntimeError(
            f"command timed out after {elapsed:.1f}s "
            f"(timeout={timeout}s): {' '.join(argv)}\n"
            f"--- partial stdout ---\n{partial_stdout!r}\n"
            f"--- partial stderr ---\n{partial_stderr!r}"
        ) from exc
