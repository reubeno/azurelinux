#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""diff-azl-repos -- diff two sets of Azure Linux repos.

Each input is a URL or local path that follows the Standard Azure Linux
Repo Layout (the same matrix consumed by ``dnf-with-azl-repos`` and
``synthesize-repodata.py``: ``base/`` + ``sdk/`` channels, ``main``,
``debuginfo``, and ``srpms`` sub-repos, per-arch where applicable).

For every (channel, kind, arch) sub-repo present under either prefix the
tool downloads ``primary.xml(.gz/.zst/.xz)`` referenced by
``repodata/repomd.xml``, extracts package metadata, and reports the
per-sub-repo diff.

Two orthogonal axes control behaviour:

  ``--compare-by {name,nevr}`` (default: ``name``)
        Defines what counts as a difference -- the *comparison key*.

        * ``name`` -- compare only the set of package names per sub-repo.
          A package whose name appears on both sides is unchanged even
          if its epoch/version/release/arch differs. A package moved
          between sub-repos shows up as remove+add in different slots.
        * ``nevr`` -- compare the set of full NEVR identities (name,
          epoch, version, release; arch intentionally collapsed). A
          version bump shows up as one remove + one add.

  ``--show {name,nevr,location}`` (default: same as ``--compare-by``)
        Defines what is *displayed* per added/removed entry --
        orthogonal to the comparison key.

        * ``name`` -- print just the bare package name.
        * ``nevr`` -- print each matching NEVR on the holding side
          (the *new* side for additions, the *old* side for removals).
        * ``location`` -- print the absolute URL of each matching RPM
          on the holding side (joined from the sub-repo URL and the
          per-package ``location_href`` in primary.xml). Rows are
          grouped under the comparison key (name or NEVR).

All four primary combinations are valid:

  ``--compare-by name --show name`` (default)
        Bare names added/removed per sub-repo.
  ``--compare-by name --show nevr``
        For each added/removed *name*, list every NEVR observed on the
        holding side -- the natural view for spotting wholesale removals
        versus partial version pruning.
  ``--compare-by name --show location``
        For each added/removed *name*, list every matching RPM's URL on
        the holding side -- handy for piping into a downloader to fetch
        every removed package, for example.
  ``--compare-by nevr --show nevr``
        Every NEVR-level addition/removal, ideal for upgrade auditing.
  ``--compare-by nevr --show location``
        Every NEVR-level addition/removal with the underlying RPM URLs
        nested per NEVR (multiple URLs when the same NEVR exists across
        archs, e.g. x86_64 + noarch).
  ``--compare-by nevr --show name``
        Coarse summary: which names had any NEVR delta.

Sub-repos absent under *both* prefixes are silently skipped (matches the
"prefix-derived, non-fatal 404" behaviour of the sibling scripts).
Sub-repos present under exactly one prefix are reported in full as
either all-new or all-removed.

Dependencies: python3-createrepo_c
"""

from __future__ import annotations

import argparse
import enum
import json
import shutil
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import createrepo_c as cr
import rpm

# `_repo_layout` is a sibling module in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo_layout import (  # noqa: E402
    KIND_DEBUGINFO,
    KIND_SRPMS,
    SUBREPOS,
)

PROG = Path(sys.argv[0]).name

DEFAULT_ARCHES: tuple[str, ...] = ("x86_64", "aarch64")
SRPM_ARCH = "src"

USER_AGENT = "diff-azl-repos/1"
HTTP_TIMEOUT = 60.0
HTTP_RETRIES = 3
HTTP_BACKOFF_BASE = 1.0     # seconds; doubled per attempt.


# ---------------------------------------------------------------------------
# Logging helpers (everything goes to stderr; stdout left clean for the
# diff report so it can be redirected/piped).
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"WARN: {msg}", file=sys.stderr, flush=True)


def die(msg: str, *, code: int = 2) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Comparison / display keys
# ---------------------------------------------------------------------------

class CompareKey(str, enum.Enum):
    """What counts as a difference (``-c/--compare-by``)."""

    NAME = "name"
    NEVR = "nevr"


class ShowKey(str, enum.Enum):
    """What is rendered for each added/removed entry (``-s/--show``).

    ``LOCATION`` is intentionally NOT a valid comparison key -- URLs
    differ trivially between two prefixes even when the underlying
    package is identical.
    """

    NAME = "name"
    NEVR = "nevr"
    LOCATION = "location"


def default_show_for(compare_by: CompareKey) -> ShowKey:
    """When ``--show`` is omitted, mirror ``--compare-by``."""
    return ShowKey(compare_by.value)


# ---------------------------------------------------------------------------
# Package identity and per-row record
# ---------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class PkgIdent:
    """Per-package comparison identity (NEVR; arch collapsed).

    Arch is intentionally NOT part of identity:
      * In ``--compare-by name`` only the name matters.
      * In ``--compare-by nevr`` the user explicitly asked to ignore
        arch. Within one sub-repo, two rows that differ only by arch
        (e.g. a noarch row alongside a $arch row of the same NEVR)
        collapse to a single entry, which is the desired behaviour for
        upgrade auditing.

    ``PkgRow`` retains arch + location_href for display purposes.
    """

    # Field order matches sort order: name first so listings cluster by
    # package, then EVR for deterministic sub-order.
    name: str
    epoch: str       # normalised: "" / None -> "0"
    version: str
    release: str

    @property
    def evr(self) -> str:
        """`[epoch:]version-release`, omitting the epoch when it is 0."""
        return (
            f"{self.version}-{self.release}"
            if self.epoch in ("", "0")
            else f"{self.epoch}:{self.version}-{self.release}"
        )

    @property
    def nevr(self) -> str:
        return f"{self.name}-{self.evr}"

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "epoch": self.epoch,
            "version": self.version,
            "release": self.release,
            "nevr": self.nevr,
        }


@dataclass(frozen=True, order=True)
class PkgRow:
    """One physical RPM row from primary.xml -- carries arch + location.

    Two rows with the same name/E/V/R but different arches yield two
    distinct PkgRows but a single :class:`PkgIdent`. The diff machinery
    uses PkgIdent for comparison and PkgRow for ``--show`` rendering.
    """

    name: str
    epoch: str
    version: str
    release: str
    arch: str
    location_href: str

    @property
    def ident(self) -> PkgIdent:
        return PkgIdent(self.name, self.epoch, self.version, self.release)

    @property
    def evr(self) -> str:
        return self.ident.evr

    @property
    def nevr(self) -> str:
        return self.ident.nevr

    def url(self, side_url: str) -> str:
        """Return the absolute URL of this RPM under *side_url*."""
        return join_url(side_url, self.location_href)


# ---------------------------------------------------------------------------
# Prefix / URL handling
# ---------------------------------------------------------------------------

def normalize_prefix(p: str) -> str:
    """Return *p* as a URL with a scheme, suitable for ``urljoin``.

    Bare paths (absolute or relative) are converted to ``file://`` URLs
    rooted at their absolute filesystem location so downstream code only
    has to deal with one URL shape.
    """
    if "://" in p:
        return p.rstrip("/")
    abs_path = Path(p).resolve()
    return "file://" + str(abs_path).rstrip("/")


def join_url(prefix: str, rel: str) -> str:
    """Join *rel* under *prefix* preserving the prefix scheme and host.

    ``urljoin`` is finicky with relative-path bases; we always append
    after a trailing slash to keep the prefix intact.
    """
    return urllib.parse.urljoin(prefix.rstrip("/") + "/", rel.lstrip("/"))


# ---------------------------------------------------------------------------
# Sub-repo enumeration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubrepoSlot:
    """One concrete (channel, kind, arch) slot, post-arch-expansion."""

    name: str        # e.g. "base", "base-debuginfo", "sdk-srpms"
    channel: str     # e.g. "base", "sdk"
    kind: str        # main | debuginfo | srpms
    arch: str        # x86_64 | aarch64 | src
    rel: str         # path under a prefix, e.g. "base/x86_64"

    def label(self) -> str:
        # Single-line human label used in the text report and as a
        # stable key in the JSON output.
        return f"{self.channel}/{self.kind}/{self.arch}"


def enumerate_slots(arches: Iterable[str], excluded_kinds: set[str]) -> list[SubrepoSlot]:
    out: list[SubrepoSlot] = []
    for sub in SUBREPOS:
        if sub.kind in excluded_kinds:
            continue
        if sub.per_arch:
            for arch in arches:
                out.append(SubrepoSlot(
                    name=sub.name,
                    channel=sub.channel,
                    kind=sub.kind,
                    arch=arch,
                    rel=sub.subpath.replace("$basearch", arch),
                ))
        else:
            out.append(SubrepoSlot(
                name=sub.name,
                channel=sub.channel,
                kind=sub.kind,
                arch=SRPM_ARCH,
                rel=sub.subpath,
            ))
    return out


# ---------------------------------------------------------------------------
# HTTP / file fetch
# ---------------------------------------------------------------------------

def build_ssl_context(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    warn("TLS certificate verification disabled (--insecure)")
    return ctx


def _http_get(
    url: str, dest: Path, ssl_context: ssl.SSLContext | None,
    *, timeout: float = HTTP_TIMEOUT, retries: int = HTTP_RETRIES,
) -> None:
    """Download *url* to *dest* with bounded retry.

    Retries on transient transport errors and HTTP 5xx. Bails immediately
    on HTTP 4xx and on permanent local-fs errors (``FileNotFoundError``
    wrapped in ``URLError``, e.g. ``file://`` to a missing file) so the
    caller can react.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: BaseException | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                req, timeout=timeout, context=ssl_context,
            ) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt < retries - 1:
                last_exc = e
                log(f"    HTTP {e.code} fetching {url}; retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, FileNotFoundError):
                raise
            if attempt < retries - 1:
                last_exc = e
                log(f"    URL error fetching {url} ({e.reason}); retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
        except (TimeoutError, OSError) as e:
            if attempt < retries - 1:
                last_exc = e
                log(f"    transport error fetching {url} ({e}); retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    # Defensive: loop only exits via return/raise above.
    if last_exc is not None:
        raise last_exc


# Outcomes of fetch_primary().
_FETCH_OK = "ok"
_FETCH_MISSING = "missing"     # repomd.xml not found -- sub-repo absent.


def fetch_primary(
    slot_url: str, cache_dir: Path, ssl_context: ssl.SSLContext | None,
) -> tuple[str, Path | None, str | None]:
    """Fetch ``repodata/repomd.xml`` + the ``primary`` record for *slot_url*.

    Returns ``(_FETCH_OK, primary_path, None)`` on success,
    ``(_FETCH_MISSING, None, None)`` when ``repomd.xml`` is absent (404 /
    ENOENT), or raises on any other transport error -- a partial fetch
    would silently understate a diff and must be loud.
    """
    repodata_dir = cache_dir / "repodata"
    repodata_dir.mkdir(parents=True, exist_ok=True)

    repomd_url = join_url(slot_url, "repodata/repomd.xml")
    repomd_path = repodata_dir / "repomd.xml"
    try:
        _http_get(repomd_url, repomd_path, ssl_context)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _FETCH_MISSING, None, None
        raise
    except urllib.error.URLError as e:
        if isinstance(e.reason, FileNotFoundError):
            return _FETCH_MISSING, None, None
        raise

    repomd = cr.Repomd()
    cr.xml_parse_repomd(str(repomd_path), repomd, lambda *_: True)

    primary_href: str | None = None
    for rec in repomd.records:
        if rec.type == "primary":
            primary_href = rec.location_href
            break
    if not primary_href:
        raise RuntimeError(
            f"{repomd_url}: no `primary` record in repomd.xml"
        )

    # Constrain the cache destination so a hostile/malformed repomd
    # can't write outside cache_dir.
    safe_rel = primary_href.lstrip("/")
    if ".." in Path(safe_rel).parts:
        raise RuntimeError(
            f"refusing to write metadata record outside cache: {primary_href!r}"
        )
    primary_url = join_url(slot_url, primary_href)
    primary_path = cache_dir / safe_rel
    _http_get(primary_url, primary_path, ssl_context)
    return _FETCH_OK, primary_path, None


# ---------------------------------------------------------------------------
# Primary.xml parsing
# ---------------------------------------------------------------------------

def load_rows(primary_path: Path) -> list[PkgRow]:
    """Return one :class:`PkgRow` per package row in *primary_path*.

    Rows are deduped by full row identity (name/E/V/R/arch/location_href)
    -- pathological repos that list the exact same row twice collapse;
    "same NEVR different arch" rows are preserved as distinct PkgRows
    so ``--show location`` can list every physical RPM URL.
    """
    seen: set[PkgRow] = set()
    out: list[PkgRow] = []

    def cb(pkg) -> None:
        if not pkg.name:
            return
        row = PkgRow(
            name=pkg.name,
            epoch=(pkg.epoch or "0"),
            version=(pkg.version or ""),
            release=(pkg.release or ""),
            arch=(pkg.arch or ""),
            location_href=(pkg.location_href or ""),
        )
        if row in seen:
            return
        seen.add(row)
        out.append(row)

    cr.xml_parse_primary(
        str(primary_path),
        pkgcb=cb,
        do_files=False,
        warningcb=lambda *_: True,
    )
    return out


# ---------------------------------------------------------------------------
# Diff per slot
# ---------------------------------------------------------------------------

@dataclass
class SlotDiff:
    slot: SubrepoSlot
    compare_by: CompareKey
    # Per-side state. None means the sub-repo was absent (no repomd.xml)
    # under that prefix.
    old_rows: list[PkgRow] | None
    new_rows: list[PkgRow] | None
    # Per-side sub-repo URLs -- needed by --show location to build
    # absolute RPM URLs from each row's location_href.
    old_url: str
    new_url: str

    # ----- low-level helpers -----

    @staticmethod
    def _names(rows: list[PkgRow] | None) -> set[str]:
        return set() if rows is None else {r.name for r in rows}

    @staticmethod
    def _idents(rows: list[PkgRow] | None) -> set[PkgIdent]:
        return set() if rows is None else {r.ident for r in rows}

    @staticmethod
    def _rows_by_name(rows: list[PkgRow] | None) -> dict[str, list[PkgRow]]:
        out: dict[str, list[PkgRow]] = {}
        for r in rows or ():
            out.setdefault(r.name, []).append(r)
        for v in out.values():
            v.sort()
        return out

    @staticmethod
    def _rows_by_ident(rows: list[PkgRow] | None) -> dict[PkgIdent, list[PkgRow]]:
        out: dict[PkgIdent, list[PkgRow]] = {}
        for r in rows or ():
            out.setdefault(r.ident, []).append(r)
        for v in out.values():
            v.sort()
        return out

    # ----- diff results -----

    def added_names(self) -> list[str]:
        if self.compare_by is CompareKey.NAME:
            return sorted(self._names(self.new_rows) - self._names(self.old_rows))
        return sorted({p.name for p in self.added_nevrs()})

    def removed_names(self) -> list[str]:
        if self.compare_by is CompareKey.NAME:
            return sorted(self._names(self.old_rows) - self._names(self.new_rows))
        return sorted({p.name for p in self.removed_nevrs()})

    def added_nevrs(self) -> list[PkgIdent]:
        old = self._idents(self.old_rows)
        new = self._idents(self.new_rows)
        if self.compare_by is CompareKey.NEVR:
            return sorted(new - old)
        added = set(self.added_names())
        return sorted({i for i in new if i.name in added})

    def removed_nevrs(self) -> list[PkgIdent]:
        old = self._idents(self.old_rows)
        new = self._idents(self.new_rows)
        if self.compare_by is CompareKey.NEVR:
            return sorted(old - new)
        removed = set(self.removed_names())
        return sorted({i for i in old if i.name in removed})

    # NEVR grouping under name (for `-c name -s nevr`).
    def nevrs_grouped_by_added_name(self) -> dict[str, list[PkgIdent]]:
        groups: dict[str, set[PkgIdent]] = {}
        for r in self.new_rows or ():
            groups.setdefault(r.name, set()).add(r.ident)
        return {n: sorted(groups.get(n, set())) for n in self.added_names()}

    def nevrs_grouped_by_removed_name(self) -> dict[str, list[PkgIdent]]:
        groups: dict[str, set[PkgIdent]] = {}
        for r in self.old_rows or ():
            groups.setdefault(r.name, set()).add(r.ident)
        return {n: sorted(groups.get(n, set())) for n in self.removed_names()}

    # Row grouping (for `-s location`).
    def rows_grouped_by_added_name(self) -> dict[str, list[PkgRow]]:
        groups = self._rows_by_name(self.new_rows)
        return {n: groups.get(n, []) for n in self.added_names()}

    def rows_grouped_by_removed_name(self) -> dict[str, list[PkgRow]]:
        groups = self._rows_by_name(self.old_rows)
        return {n: groups.get(n, []) for n in self.removed_names()}

    def rows_grouped_by_added_nevr(self) -> dict[PkgIdent, list[PkgRow]]:
        groups = self._rows_by_ident(self.new_rows)
        return {i: groups.get(i, []) for i in self.added_nevrs()}

    def rows_grouped_by_removed_nevr(self) -> dict[PkgIdent, list[PkgRow]]:
        groups = self._rows_by_ident(self.old_rows)
        return {i: groups.get(i, []) for i in self.removed_nevrs()}

    @property
    def status(self) -> str:
        if self.old_rows is None and self.new_rows is None:
            return "absent-both"
        if self.old_rows is None:
            return "new-only"
        if self.new_rows is None:
            return "old-only"
        if self.compare_by is CompareKey.NEVR:
            unchanged = self._idents(self.old_rows) == self._idents(self.new_rows)
        else:
            unchanged = self._names(self.old_rows) == self._names(self.new_rows)
        return "unchanged" if unchanged else "changed"


def _max_evr_per_name_arch(
    rows: list[PkgRow] | None,
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Return ``{(name, arch): max (epoch, version, release)}`` for *rows*.

    Uses ``rpm.labelCompare`` for proper RPM version ordering (epoch,
    tilde, caret semantics). Returns an empty mapping when *rows* is
    falsy.
    """
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for r in rows or ():
        key = (r.name, r.arch)
        evr = (r.epoch, r.version, r.release)
        cur = out.get(key)
        if cur is None or rpm.labelCompare(evr, cur) > 0:
            out[key] = evr
    return out


def _suppress_stale(
    rows: list[PkgRow] | None,
    reference_rows: list[PkgRow] | None,
) -> list[PkgRow] | None:
    """Drop rows from *rows* whose EVR is strictly older than the max
    EVR of the same ``(name, arch)`` observed in *reference_rows*.

    Rows whose ``(name, arch)`` does not appear in *reference_rows* are
    retained unchanged -- the filter only suppresses *stale leftover
    builds*, never genuine name-level adds/removes.

    Returns ``None`` unchanged so the "absent sub-repo" sentinel is
    preserved end-to-end.
    """
    if rows is None or not reference_rows:
        return rows
    ref_max = _max_evr_per_name_arch(reference_rows)
    kept: list[PkgRow] = []
    for r in rows:
        ref = ref_max.get((r.name, r.arch))
        if ref is not None and rpm.labelCompare(
            (r.epoch, r.version, r.release), ref
        ) < 0:
            continue
        kept.append(r)
    return kept


def diff_slot(
    slot: SubrepoSlot,
    old_prefix: str, new_prefix: str,
    cache_root: Path,
    ssl_context: ssl.SSLContext | None,
    compare_by: CompareKey,
    suppress_stale_new: bool = False,
    suppress_stale_old: bool = False,
) -> SlotDiff:
    old_url = join_url(old_prefix, slot.rel)
    new_url = join_url(new_prefix, slot.rel)
    log(f"  {slot.label()}")
    log(f"    old <- {old_url}")
    log(f"    new <- {new_url}")

    old_cache = cache_root / "old" / slot.label().replace("/", "_")
    new_cache = cache_root / "new" / slot.label().replace("/", "_")

    old_status, old_primary, _ = fetch_primary(old_url, old_cache, ssl_context)
    new_status, new_primary, _ = fetch_primary(new_url, new_cache, ssl_context)

    old_rows = load_rows(old_primary) if old_status == _FETCH_OK else None
    new_rows = load_rows(new_primary) if new_status == _FETCH_OK else None

    # Stale-suppression filters: applied *before* the diff so all
    # downstream counts, listings, and JSON output reflect the filtered
    # view. Each side is filtered against the *unfiltered* rows of the
    # other side so the operations are symmetric and order-independent.
    if suppress_stale_new or suppress_stale_old:
        orig_old_rows = old_rows
        orig_new_rows = new_rows
        if suppress_stale_new:
            new_rows = _suppress_stale(new_rows, orig_old_rows)
        if suppress_stale_old:
            old_rows = _suppress_stale(old_rows, orig_new_rows)

    return SlotDiff(
        slot=slot, compare_by=compare_by,
        old_rows=old_rows, new_rows=new_rows,
        old_url=old_url, new_url=new_url,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_count(rows: list[PkgRow] | None) -> str:
    return "(absent)" if rows is None else f"({len(rows)} pkgs)"


def _render_side_text(d: SlotDiff, side: str, show: ShowKey) -> list[str]:
    """Build the indented body lines (without the leading bullet) for the
    added or removed side of one sub-repo, honouring (compare_by, show).
    """
    is_added = (side == "added")
    side_url = d.new_url if is_added else d.old_url
    lines: list[str] = []

    if show is ShowKey.NAME:
        names = d.added_names() if is_added else d.removed_names()
        lines.extend(names)
        return lines

    if show is ShowKey.NEVR:
        if d.compare_by is CompareKey.NEVR:
            idents = d.added_nevrs() if is_added else d.removed_nevrs()
            for p in idents:
                lines.append(p.nevr)
        else:
            grouped = (d.nevrs_grouped_by_added_name() if is_added
                       else d.nevrs_grouped_by_removed_name())
            names = d.added_names() if is_added else d.removed_names()
            for n in names:
                lines.append(n)
                for p in grouped[n]:
                    lines.append(f"    {p.evr}")
        return lines

    # ShowKey.LOCATION
    if d.compare_by is CompareKey.NEVR:
        grouped_nevr = (d.rows_grouped_by_added_nevr() if is_added
                        else d.rows_grouped_by_removed_nevr())
        idents = d.added_nevrs() if is_added else d.removed_nevrs()
        for i in idents:
            lines.append(i.nevr)
            for r in grouped_nevr[i]:
                lines.append(f"    {r.url(side_url)}")
    else:
        grouped_name = (d.rows_grouped_by_added_name() if is_added
                        else d.rows_grouped_by_removed_name())
        names = d.added_names() if is_added else d.removed_names()
        for n in names:
            lines.append(n)
            for r in grouped_name[n]:
                lines.append(f"    {r.url(side_url)}")
    return lines


def _count_label(d: SlotDiff, show: ShowKey, side: str) -> str:
    names_n = (len(d.added_names()) if side == "added"
               else len(d.removed_names()))
    nevrs_n = (len(d.added_nevrs()) if side == "added"
               else len(d.removed_nevrs()))
    rows_n: int | None = None
    if show is ShowKey.LOCATION:
        if d.compare_by is CompareKey.NEVR:
            grouped = (d.rows_grouped_by_added_nevr() if side == "added"
                       else d.rows_grouped_by_removed_nevr())
        else:
            grouped = (d.rows_grouped_by_added_name() if side == "added"
                       else d.rows_grouped_by_removed_name())
        rows_n = sum(len(v) for v in grouped.values())

    if show is ShowKey.LOCATION:
        if d.compare_by is CompareKey.NAME:
            return f"{names_n} names / {nevrs_n} NEVRs / {rows_n} RPMs"
        return f"{nevrs_n} NEVRs / {rows_n} RPMs"
    if show is ShowKey.NEVR and d.compare_by is CompareKey.NAME:
        return f"{names_n} names / {nevrs_n} NEVRs"
    if show is ShowKey.NEVR:
        return f"{nevrs_n} NEVRs"
    if d.compare_by is CompareKey.NEVR:
        return f"{names_n} names ({nevrs_n} NEVRs underlying)"
    return f"{names_n}"


def report_text(
    diffs: list[SlotDiff], *,
    show: ShowKey, show_unchanged: bool,
) -> None:
    total_added_names = 0
    total_removed_names = 0
    total_added_nevrs = 0
    total_removed_nevrs = 0
    shown = 0
    for d in diffs:
        if d.status == "absent-both":
            continue
        if d.status == "unchanged" and not show_unchanged:
            continue
        shown += 1
        print(
            f"== {d.slot.label()} ==  "
            f"old {_fmt_count(d.old_rows)}, new {_fmt_count(d.new_rows)}"
        )
        if d.status == "unchanged":
            print("  no changes")
            print()
            continue
        if d.status == "old-only":
            print("  sub-repo present only on old side; ALL packages "
                  "would be REMOVED")
        elif d.status == "new-only":
            print("  sub-repo present only on new side; ALL packages "
                  "are ADDED")

        total_added_names += len(d.added_names())
        total_added_nevrs += len(d.added_nevrs())
        print(f"  + added ({_count_label(d, show, 'added')}):")
        for line in _render_side_text(d, "added", show):
            print(f"      {line}")

        total_removed_names += len(d.removed_names())
        total_removed_nevrs += len(d.removed_nevrs())
        print(f"  - removed ({_count_label(d, show, 'removed')}):")
        for line in _render_side_text(d, "removed", show):
            print(f"      {line}")
        print()

    if shown == 0:
        print("(no sub-repos to compare)")
    print(
        f"summary: +{total_added_names} names / {total_added_nevrs} NEVRs added, "
        f"-{total_removed_names} names / {total_removed_nevrs} NEVRs removed "
        f"across {shown} sub-repo(s)"
    )


def _row_json(r: PkgRow, side_url: str) -> dict:
    return {
        "arch": r.arch,
        "location_href": r.location_href,
        "url": r.url(side_url),
    }


def _json_side(d: SlotDiff, side: str, show: ShowKey) -> list:
    is_added = (side == "added")
    side_url = d.new_url if is_added else d.old_url

    if show is ShowKey.NAME:
        return d.added_names() if is_added else d.removed_names()

    if show is ShowKey.NEVR:
        if d.compare_by is CompareKey.NEVR:
            idents = d.added_nevrs() if is_added else d.removed_nevrs()
            return [p.to_json() for p in idents]
        grouped = (d.nevrs_grouped_by_added_name() if is_added
                   else d.nevrs_grouped_by_removed_name())
        return [
            {"name": n, "nevrs": [p.to_json() for p in ps]}
            for n, ps in grouped.items()
        ]

    # ShowKey.LOCATION
    if d.compare_by is CompareKey.NEVR:
        grouped_nevr = (d.rows_grouped_by_added_nevr() if is_added
                        else d.rows_grouped_by_removed_nevr())
        return [
            {**i.to_json(), "rows": [_row_json(r, side_url) for r in rows]}
            for i, rows in grouped_nevr.items()
        ]
    grouped_name = (d.rows_grouped_by_added_name() if is_added
                    else d.rows_grouped_by_removed_name())
    return [
        {"name": n, "rows": [_row_json(r, side_url) for r in rows]}
        for n, rows in grouped_name.items()
    ]


def report_json(
    diffs: list[SlotDiff], *, compare_by: CompareKey, show: ShowKey,
) -> None:
    payload: dict = {
        "compare_by": compare_by.value,
        "show": show.value,
        "subrepos": [],
    }
    total_added_names = 0
    total_removed_names = 0
    total_added_nevrs = 0
    total_removed_nevrs = 0
    for d in diffs:
        if d.status == "absent-both":
            continue
        added = _json_side(d, "added", show) if d.status != "unchanged" else []
        removed = _json_side(d, "removed", show) if d.status != "unchanged" else []
        total_added_names += len(d.added_names())
        total_removed_names += len(d.removed_names())
        total_added_nevrs += len(d.added_nevrs())
        total_removed_nevrs += len(d.removed_nevrs())
        payload["subrepos"].append({
            "label": d.slot.label(),
            "channel": d.slot.channel,
            "kind": d.slot.kind,
            "arch": d.slot.arch,
            "rel": d.slot.rel,
            "old_url": d.old_url,
            "new_url": d.new_url,
            "status": d.status,
            "old_count": None if d.old_rows is None else len(d.old_rows),
            "new_count": None if d.new_rows is None else len(d.new_rows),
            "added": added,
            "removed": removed,
        })
    payload["summary"] = {
        "total_added_names": total_added_names,
        "total_removed_names": total_removed_names,
        "total_added_nevrs": total_added_nevrs,
        "total_removed_nevrs": total_removed_nevrs,
        "subrepos_compared": len(payload["subrepos"]),
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--old", required=True, metavar="PREFIX",
        help=(
            "URL or local path prefix for the 'old' / baseline set of "
            "repos. Must follow the Standard Azure Linux Repo Layout."
        ),
    )
    parser.add_argument(
        "--new", required=True, metavar="PREFIX",
        help=(
            "URL or local path prefix for the 'new' / candidate set of "
            "repos. Must follow the Standard Azure Linux Repo Layout."
        ),
    )
    parser.add_argument(
        "-c", "--compare-by", choices=[k.value for k in CompareKey],
        default=CompareKey.NAME.value,
        help=(
            "What counts as a difference (comparison key). "
            "'name' (default): packages with the same name are unchanged "
            "regardless of EVR. 'nevr': compare full NEVR identities "
            "(arch collapsed); a version bump is one remove + one add."
        ),
    )
    parser.add_argument(
        "-s", "--show", choices=[k.value for k in ShowKey],
        default=None,
        help=(
            "What to print for each added/removed entry (display key). "
            "'name': bare package name. 'nevr': each matching NEVR on "
            "the holding side. 'location': the absolute URL of each "
            "matching RPM on the holding side (new side for adds, old "
            "side for removals). Defaults to the value of --compare-by; "
            "'location' is only valid as a --show option."
        ),
    )
    parser.add_argument(
        "--arch", action="append", default=None, metavar="ARCH",
        help=(
            "Per-arch sub-repo to include. Repeatable. Default: "
            f"{', '.join(DEFAULT_ARCHES)}."
        ),
    )
    parser.add_argument(
        "--no-debuginfo", action="store_true",
        help="Skip debuginfo sub-repos.",
    )
    parser.add_argument(
        "--no-srpms", action="store_true",
        help="Skip srpm sub-repos.",
    )
    parser.add_argument(
        "--show-unchanged", action="store_true",
        help=(
            "Also report sub-repos with no differences under the "
            "active --compare-by key (text mode only; JSON output "
            "always elides absent-both sub-repos and always emits "
            "unchanged ones with empty added/removed)."
        ),
    )
    parser.add_argument(
        "--suppress-stale-new", action="store_true",
        help=(
            "Drop NEVRs from the 'new' side whose (name, arch) also "
            "appears in 'old' and whose EVR is strictly older than the "
            "latest EVR of that (name, arch) in 'old'. Suppresses stale "
            "leftover builds in 'new' from showing up as additions. "
            "Only valid with --compare-by nevr."
        ),
    )
    parser.add_argument(
        "--suppress-stale-old", action="store_true",
        help=(
            "Symmetric counterpart of --suppress-stale-new: drop NEVRs "
            "from 'old' whose (name, arch) also appears in 'new' and "
            "whose EVR is strictly older than the latest EVR of that "
            "(name, arch) in 'new'. Only valid with --compare-by nevr."
        ),
    )
    parser.add_argument(
        "-O", "--output-format", choices=("text", "json"), default="text",
        help="Output format on stdout. Default: text.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS certificate verification for https:// prefixes.",
    )
    parser.add_argument(
        "--cache-dir", metavar="DIR", default=None,
        help=(
            "Reusable cache directory for downloaded repodata. Default: "
            "a private tmp dir cleaned up on exit."
        ),
    )
    args = parser.parse_args(argv)

    args.arches = tuple(args.arch) if args.arch else DEFAULT_ARCHES
    excluded: set[str] = set()
    if args.no_debuginfo:
        excluded.add(KIND_DEBUGINFO)
    if args.no_srpms:
        excluded.add(KIND_SRPMS)
    args.excluded_kinds = excluded
    args.compare_by_key = CompareKey(args.compare_by)
    args.show_key = (
        ShowKey(args.show) if args.show
        else default_show_for(args.compare_by_key)
    )
    if (
        (args.suppress_stale_new or args.suppress_stale_old)
        and args.compare_by_key is not CompareKey.NEVR
    ):
        die(
            "--suppress-stale-new / --suppress-stale-old require "
            "--compare-by nevr"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = parse_args(argv)

    old_prefix = normalize_prefix(args.old)
    new_prefix = normalize_prefix(args.new)
    if old_prefix == new_prefix:
        warn(f"--old and --new resolved to the same prefix ({old_prefix}); "
             "the diff will be empty by construction")

    ssl_context = build_ssl_context(args.insecure)
    slots = enumerate_slots(args.arches, args.excluded_kinds)
    log(f"{PROG}: comparing {len(slots)} sub-repo slot(s) "
        f"(compare-by={args.compare_by_key.value}, show={args.show_key.value})")
    log(f"  old = {old_prefix}")
    log(f"  new = {new_prefix}")

    diffs: list[SlotDiff] = []

    def _run(cache_root: Path) -> None:
        for slot in slots:
            diffs.append(
                diff_slot(
                    slot, old_prefix, new_prefix, cache_root, ssl_context,
                    compare_by=args.compare_by_key,
                    suppress_stale_new=args.suppress_stale_new,
                    suppress_stale_old=args.suppress_stale_old,
                )
            )

    if args.cache_dir:
        cache_root = Path(args.cache_dir)
        cache_root.mkdir(parents=True, exist_ok=True)
        _run(cache_root)
    else:
        with tempfile.TemporaryDirectory(prefix="diff-azl-repos.") as tmp:
            _run(Path(tmp))

    if args.output_format == "json":
        report_json(
            diffs, compare_by=args.compare_by_key, show=args.show_key,
        )
    else:
        report_text(
            diffs, show=args.show_key, show_unchanged=args.show_unchanged,
        )

    # Exit non-zero if any present-on-both-sides sub-repo had any
    # difference, OR any sub-repo was present on only one side. Useful
    # in CI / scripted comparisons. (absent-both is not an error.)
    for d in diffs:
        if d.status in ("changed", "old-only", "new-only"):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
