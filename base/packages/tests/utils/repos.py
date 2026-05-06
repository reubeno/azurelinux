# SPDX-License-Identifier: MIT
"""Repo definitions and CLI parsing.

Two input forms are supported, both produce a list of :class:`Repo`:

1. ``--repos-file path.repo`` — a standard yum/dnf ``.repo`` ini file.
   The file is parsed with :mod:`configparser`. Each section becomes
   one repo; required keys are ``baseurl``, plus a custom ``kind``
   key (``binary`` / ``srpm`` / ``debuginfo``) since the dnf format
   has no equivalent. The section name is the repo name.

2. ``--repo name=...,kind=...,url=...`` — inline form for ad-hoc
   invocations and CI matrix jobs that don't want to ship a separate
   .repo file.

Both forms accept ``$basearch`` / ``$arch`` / ``$releasever`` in URLs;
substitution happens at fetch time inside librepo.
"""

from __future__ import annotations

import configparser
import hashlib
from dataclasses import dataclass
from pathlib import Path

from .types import ALL_REPO_KINDS, RepoKind


@dataclass(frozen=True)
class Repo:
    """A logical repository under test.

    The ``fingerprint`` is a stable short hash that uniquely identifies
    this repo for cache-keying purposes. It does NOT cover ``arch`` or
    ``releasever`` — those live in cache-path components above the repo
    fingerprint, so the same repo's metadata across two arches lands in
    two different cache subdirs.
    """

    name: str
    kind: RepoKind
    url: str

    @property
    def fingerprint(self) -> str:
        """Stable short hash over (name, kind, url) for cache paths."""
        h = hashlib.sha256()
        h.update(self.name.encode())
        h.update(b"\0")
        h.update(self.kind.encode())
        h.update(b"\0")
        h.update(self.url.encode())
        return h.hexdigest()[:16]


class RepoSpecError(ValueError):
    """Raised when a ``--repo`` flag value or repos-file is malformed."""


_REQUIRED_KEYS = ("name", "kind", "url")


def _validate(name: str, kind: str, url: str, source: str) -> Repo:
    if not name:
        raise RepoSpecError(f"{source}: name must be non-empty")
    if kind not in ALL_REPO_KINDS:
        raise RepoSpecError(
            f"{source}: kind {kind!r} is invalid; "
            f"must be one of {', '.join(ALL_REPO_KINDS)}"
        )
    if not url:
        raise RepoSpecError(f"{source}: url must be non-empty")
    return Repo(name=name, kind=kind, url=url)  # type: ignore[arg-type]


def parse_repo_spec(raw: str) -> Repo:
    """Parse a ``--repo name=...,kind=...,url=...`` value into a :class:`Repo`.

    The first ``=`` in each comma-segment separates key from value, so
    URLs containing ``=`` are tolerated. Keys are case-sensitive.
    """
    if not raw or not raw.strip():
        raise RepoSpecError("--repo value is empty")
    fields: dict[str, str] = {}
    for segment in raw.split(","):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            raise RepoSpecError(
                f"--repo segment {segment!r} is not of the form key=value"
            )
        key, _, value = segment.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            raise RepoSpecError(f"--repo segment {segment!r} has empty key")
        if key in fields:
            raise RepoSpecError(f"--repo key {key!r} specified twice")
        fields[key] = value

    missing = [k for k in _REQUIRED_KEYS if k not in fields]
    if missing:
        raise RepoSpecError(
            f"--repo {raw!r} is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(set(fields) - set(_REQUIRED_KEYS))
    if extra:
        raise RepoSpecError(
            f"--repo {raw!r} has unknown key(s): {', '.join(extra)}. "
            f"Allowed: {', '.join(_REQUIRED_KEYS)}."
        )
    return _validate(fields["name"], fields["kind"], fields["url"], f"--repo {raw!r}")


def parse_repos_file(path: Path) -> list[Repo]:
    """Parse a ``.repo``-style ini file into a list of :class:`Repo`.

    The format is the standard yum/dnf one with one extension: every
    section MUST include a ``kind`` key (one of ``binary`` / ``srpm`` /
    ``debuginfo``) since the dnf format has no equivalent.

    Example::

        [base]
        name=Azure Linux base
        baseurl=https://example.com/base/$basearch/
        kind=binary

        [base-srpms]
        baseurl=https://example.com/base-srpms/
        kind=srpm

    The repo name is taken from the section header (``[base]``); the
    optional ``name=`` key is ignored (kept only for compatibility
    with hand-edited dnf .repo files).
    """
    cp = configparser.ConfigParser(interpolation=None)
    try:
        with open(path) as fh:
            cp.read_file(fh)
    except (OSError, configparser.Error) as exc:
        raise RepoSpecError(f"failed to read --repos-file {path}: {exc}") from exc

    repos: list[Repo] = []
    seen: set[str] = set()
    for section in cp.sections():
        if section in seen:
            raise RepoSpecError(
                f"--repos-file {path}: section [{section}] appears twice"
            )
        seen.add(section)
        url = cp[section].get("baseurl", "").strip()
        kind = cp[section].get("kind", "").strip()
        repos.append(_validate(
            section, kind, url, f"--repos-file {path} [{section}]"
        ))
    return repos


def collect_repos(
    *, inline: list[str], file_paths: list[str]
) -> list[Repo]:
    """Combine inline ``--repo`` and file-form ``--repos-file`` inputs.

    Repo *names* must be globally unique across all inputs (regardless
    of source). Earlier versions of this code allowed two repos to
    share a base name as long as their kinds differed, but that
    invariant proved unenforceable downstream:

    * the rendered ``.repo`` file uses ``[name]`` as the section
      header — duplicate sections cause dnf to merge or reject;
    * fixture lookups (``require_named_repos``, repoclosure result
      attribution) key on name alone and silently overwrote the
      earlier entry;
    * dnf5 ``repoclosure --json`` reports source repos by name only,
      so per-repo filtering can't disambiguate same-named binary vs
      srpm repos.

    The conventional naming is ``base`` for the binary repo and
    ``base-srpms`` for the matching SRPM repo — distinct names, no
    behaviour change for well-formed inputs.
    """
    repos: list[Repo] = []
    seen: dict[str, str] = {}

    def _add(repo: Repo, source: str) -> None:
        if repo.name in seen:
            raise RepoSpecError(
                f"repo name={repo.name!r} specified more than once "
                f"(previously: {seen[repo.name]!r}, now: {source!r}). "
                f"Repo names must be globally unique — pick distinct "
                f"names (e.g. 'base' for the binary repo and "
                f"'base-srpms' for the matching SRPM repo)."
            )
        seen[repo.name] = source
        repos.append(repo)

    for raw in inline:
        _add(parse_repo_spec(raw), f"--repo {raw!r}")
    for fp in file_paths:
        for r in parse_repos_file(Path(fp)):
            _add(r, f"--repos-file {fp} [{r.name}]")

    return repos
