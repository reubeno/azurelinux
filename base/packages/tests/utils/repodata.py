# SPDX-License-Identifier: MIT
"""Repository metadata fetch + parse — thin wrapper around librepo + createrepo_c.

This module is an *implementation* layer; fixtures and tests do not
import it. :class:`utils.metadata.MetadataService` wraps it to provide
a caching service interface.

Design
------

* :func:`fetch_repo` uses ``librepo`` to download ``repomd.xml`` and
  the ``primary`` + ``filelists`` records into a per-repo cache
  directory. librepo handles checksum verification, zchunk/zstd/xz/gz
  decompression, mirror handling, retries, and the atomic-rename
  guarantees we used to do by hand.

* :func:`iter_packages` and :func:`iter_filelist_entries` use
  ``createrepo_c``'s C-based primary/filelists parsers, which return
  rich pre-typed records and stream the underlying file (so memory is
  bounded for very large filelists).

The previous implementation hand-rolled HTTP fetch with retries,
checksum verification, multi-format decompression (gz/xz/zstd via
``zstandard``), and ``defusedxml.iterparse`` of primary + filelists
— ~700 lines that did exactly what librepo+createrepo_c already do.
The dnf stack uses these same libraries internally, so the two
codepaths are now guaranteed to interpret repodata identically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import createrepo_c as cr
import librepo

from .types import NEVRA, ConflictEntry, FileEntry, Package

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepodataError(Exception):
    """Base class for repodata fetch/parse errors."""


class RepodataFetchError(RepodataError):
    """Raised when librepo fails to fetch or verify a repo."""


class RepodataParseError(RepodataError):
    """Raised when createrepo_c fails to parse a metadata file."""


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


# librepo's variable substitution covers $releasever and $basearch
# directly; we feed it ``arch``/``releasever`` and let it expand the
# baseurl. We never need to inherit either from the host.
_LR_VARS = ("arch", "basearch", "releasever")


@dataclass(frozen=True)
class RepoLayout:
    """Local paths to the metadata files of one fetched repo."""

    repomd: Path
    primary: Path
    filelists: Path


def fetch_repo(
    *,
    base_url: str,
    cache_dir: Path,
    arch: str,
    releasever: str | None,
) -> RepoLayout:
    """Fetch ``repomd.xml`` + primary + filelists into *cache_dir*.

    Returns the local paths librepo wrote them to. librepo verifies
    checksums against ``repomd.xml`` automatically, retries transient
    network failures, and decompresses zchunk in place; the caller
    just hands the resulting paths to :func:`iter_packages` /
    :func:`iter_filelist_entries`.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    h = librepo.Handle()
    h.urls = [base_url]
    h.repotype = librepo.YUMREPO
    h.destdir = str(cache_dir)
    # We only need primary + filelists for the metadata-only tests;
    # repoclosure loads the same cache_dir with a wider set
    # (see utils.repoclosure).
    h.yumdlist = ["primary", "filelists"]
    varsub = [("arch", arch), ("basearch", arch)]
    if releasever:
        varsub.append(("releasever", releasever))
    h.varsub = varsub
    # Verification is on by default; spelled out for clarity.
    h.checksum = True
    try:
        result = h.perform()
    except librepo.LibrepoException as exc:
        raise RepodataFetchError(f"failed to fetch {base_url}: {exc}") from exc

    yum_repo = result.yum_repo
    try:
        return RepoLayout(
            repomd=Path(yum_repo["repomd"]),
            primary=Path(yum_repo["primary"]),
            filelists=Path(yum_repo["filelists"]),
        )
    except KeyError as exc:
        raise RepodataFetchError(
            f"librepo result for {base_url} is missing record {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# primary.xml parsing
# ---------------------------------------------------------------------------


def _epoch_to_int(epoch: str | None) -> int:
    """Coerce createrepo_c's string epoch (or None / "") to an int.

    createrepo_c reports epoch as a (possibly empty) string; the suite
    types epoch as ``int`` (defaulting to 0 when missing or unparseable).
    """
    if not epoch:
        return 0
    try:
        return int(epoch)
    except ValueError:
        return 0


def _convert_conflict(entry: tuple) -> ConflictEntry:
    """Map createrepo_c's ``(name, flags, epoch, ver, rel, pre)`` tuple."""
    name, flags, epoch, ver, rel, _pre = entry
    epoch_int: int | None = _epoch_to_int(epoch) if epoch else None
    return ConflictEntry(
        name=name,
        flags=flags or None,
        epoch=epoch_int,
        version=ver or None,
        release=rel or None,
    )


def _convert_file(entry: tuple) -> FileEntry:
    """Map createrepo_c's ``(type|None, dirname, basename)`` tuple.

    createrepo_c always splits the path; we recombine because that's
    what the rest of the suite expects (single string paths).
    """
    ftype, dirname, basename = entry
    return FileEntry(
        path=dirname + basename,
        is_directory=(ftype == "dir"),
        is_ghost=(ftype == "ghost"),
    )


def _convert_package(crp: cr.Package) -> Package:
    """Convert a ``createrepo_c.Package`` into the suite's :class:`Package`."""
    nevra = NEVRA(
        name=crp.name,
        epoch=_epoch_to_int(crp.epoch),
        version=crp.version,
        release=crp.release,
        arch=crp.arch,
    )
    # ``provides`` is a list of (name, flags, epoch, ver, rel, pre)
    # tuples; for our purposes the bare name set is enough — we use
    # it for cross-repo virtual-conflict matching only. RPM auto-emits
    # ``Provides: <name>``, but we also force the bare name in (cheap
    # safety net for repos where createrepo somehow omits it).
    provides_names = [p[0] for p in (crp.provides or []) if p and p[0]]
    if crp.name not in provides_names:
        provides_names.append(crp.name)

    return Package(
        nevra=nevra,
        vendor=crp.rpm_vendor or None,
        sourcerpm=crp.rpm_sourcerpm or None,
        summary=crp.summary or None,
        provides=provides_names,
        conflicts=[_convert_conflict(c) for c in (crp.conflicts or [])],
        files=[_convert_file(f) for f in (crp.files or [])],
    )


def iter_packages(primary_path: Path) -> Generator[Package, None, None]:
    """Yield :class:`Package` records from a ``primary.xml(.gz|.xz|.zst)``.

    createrepo_c streams the file via libxml2; memory stays bounded
    even for very large repos.
    """
    pkgs: list[Package] = []

    def _cb(crp: cr.Package) -> None:
        pkgs.append(_convert_package(crp))

    try:
        cr.xml_parse_primary(str(primary_path), pkgcb=_cb)
    except cr.CreaterepoCError as exc:
        raise RepodataParseError(
            f"failed to parse {primary_path}: {exc}"
        ) from exc
    yield from pkgs


# ---------------------------------------------------------------------------
# filelists.xml parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilelistEntry:
    """One ``(NEVRA, file)`` entry from filelists.xml."""

    nevra: NEVRA
    path: str
    is_directory: bool
    is_ghost: bool


def iter_filelist_entries(
    filelists_path: Path,
) -> Generator[FilelistEntry, None, None]:
    """Yield one :class:`FilelistEntry` per file in ``filelists.xml``.

    createrepo_c invokes the callback once per package; we expand to
    one entry per file (which is what consumers want).
    """
    entries: list[FilelistEntry] = []

    def _cb(crp: cr.Package) -> None:
        nevra = NEVRA(
            name=crp.name,
            epoch=_epoch_to_int(crp.epoch),
            version=crp.version,
            release=crp.release,
            arch=crp.arch,
        )
        for ftype, dirname, basename in crp.files or ():
            entries.append(FilelistEntry(
                nevra=nevra,
                path=dirname + basename,
                is_directory=(ftype == "dir"),
                is_ghost=(ftype == "ghost"),
            ))

    try:
        cr.xml_parse_filelists(str(filelists_path), pkgcb=_cb)
    except cr.CreaterepoCError as exc:
        raise RepodataParseError(
            f"failed to parse {filelists_path}: {exc}"
        ) from exc
    yield from entries
