# SPDX-License-Identifier: MIT
"""Repo definitions and ``--repo`` CLI parsing.

A ``--repo`` flag has the form::

    --repo name=base,kind=binary,url=https://example.com/base/$basearch/

The implementation deliberately accepts only this strict ``key=value``
form (comma-separated) so URLs containing arbitrary characters never
collide with the parser. Whitespace around keys and values is stripped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

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
    """Raised when a ``--repo`` flag value is malformed."""


_REQUIRED_KEYS = ("name", "kind", "url")


def parse_repo_spec(raw: str) -> Repo:
    """Parse a ``--repo`` flag value (``name=...,kind=...,url=...``) into a Repo.

    The first ``=`` in each comma-segment separates key from value, so
    URLs containing ``=`` are tolerated. Keys are case-sensitive.
    """
    if not raw or not raw.strip():
        raise RepoSpecError("--repo value is empty")

    fields: dict[str, str] = {}
    # Split on commas at the top level. URLs typically don't contain
    # commas; if a future URL ever does, the strict format protects us
    # from having to escape — at worst the user picks a different URL
    # form (e.g. one without commas). We document this caveat in
    # docs/architecture.md.
    for segment in raw.split(","):
        segment = segment.strip()
        if not segment:
            continue
        if "=" not in segment:
            raise RepoSpecError(
                f"--repo segment {segment!r} is not of the form key=value"
            )
        key, _, value = segment.partition("=")
        key = key.strip()
        value = value.strip()
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

    name = fields["name"]
    kind = fields["kind"]
    url = fields["url"]

    if not name:
        raise RepoSpecError(f"--repo {raw!r}: name must be non-empty")
    if kind not in ALL_REPO_KINDS:
        raise RepoSpecError(
            f"--repo {raw!r}: kind {kind!r} is invalid; "
            f"must be one of {', '.join(ALL_REPO_KINDS)}"
        )
    if not url:
        raise RepoSpecError(f"--repo {raw!r}: url must be non-empty")

    return Repo(name=name, kind=kind, url=url)  # type: ignore[arg-type]


def parse_repo_specs(raws: list[str]) -> list[Repo]:
    """Parse a list of ``--repo`` flag values.

    Repo *names* must be globally unique across the whole CLI
    invocation. Earlier versions of this parser allowed two repos to
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

    Tightening the parser is the single fix that keeps every
    consumer honest. The conventional naming is ``base`` for the
    binary repo and ``base-srpms`` for the matching SRPM repo —
    distinct names, no behavior change for well-formed inputs.
    """
    repos: list[Repo] = []
    seen: dict[str, str] = {}
    for raw in raws:
        repo = parse_repo_spec(raw)
        if repo.name in seen:
            raise RepoSpecError(
                f"--repo name={repo.name!r} specified more than once "
                f"(previously: {seen[repo.name]!r}). Repo names must be "
                f"globally unique across all --repo flags — pick distinct "
                f"names (e.g. 'base' for the binary repo and 'base-srpms' "
                f"for the matching SRPM repo)."
            )
        seen[repo.name] = raw
        repos.append(repo)
    if not repos:
        raise RepoSpecError("at least one --repo argument is required")
    return repos
