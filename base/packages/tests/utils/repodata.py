# SPDX-License-Identifier: MIT
"""Repository metadata fetch + stream-parse.

This module is an *implementation* layer — fixtures and tests do not
import it directly. :class:`utils.metadata.MetadataService` wraps it
to provide a clean, caching service interface.

Responsibilities:

* Fetch ``repodata/repomd.xml`` from a repo's base URL.
* From repomd, identify the ``primary`` and ``filelists`` records
  (preferring ``primary_zck`` / ``filelists_zck`` -> ``_xml`` ->
  ``_gz`` based on what the local environment can decompress).
* Download those records to a fingerprint-keyed cache directory and
  stream-parse them with ``xml.etree.ElementTree.iterparse`` (no
  full-DOM load — filelists in particular can be very large).
* Yield typed records (:class:`Package`, :class:`FileEntry`).

Network errors raise :class:`RepodataFetchError`. Parse errors raise
:class:`RepodataParseError`. The caller (``MetadataService``) is
expected to translate these into ``pytest.fail`` calls so test output
is clean.

URL substitution
----------------

The caller passes in the *substituted* URL (i.e. ``$basearch`` already
replaced with the chosen arch and ``$releasever`` with the chosen
release). dnf still gets the unsubstituted URL — but for direct
fetches we have no dnf in the loop, so substitution is done in
:func:`substitute_url`.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import logging
import lzma
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Iterable

# defusedxml's parse/iterparse harden against XML entity-expansion and
# external-entity attacks. Repodata is fetched over the network and
# the parse layer has historically been a soft underbelly of dnf-style
# tooling; using defusedxml here is cheap insurance even for
# well-known mirrors. Element instances and ParseError remain stdlib
# types — defusedxml only swaps the entry-point parsers.
from defusedxml.ElementTree import iterparse as _iterparse_safe
from defusedxml.ElementTree import parse as _parse_safe

from .types import NEVRA, ConflictEntry, FileEntry, Package

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RepodataError(Exception):
    """Base class for repodata fetch/parse errors."""


class RepodataFetchError(RepodataError):
    """Raised when a network fetch (repomd or a metadata file) fails."""


class RepodataParseError(RepodataError):
    """Raised when XML parsing of a metadata file fails."""


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def substitute_url(url: str, *, arch: str, releasever: str | None) -> str:
    """Substitute ``$basearch`` / ``$arch`` / ``$releasever`` in *url*.

    Trailing slashes are normalized so we can safely ``urljoin`` against
    the result.
    """
    out = url
    out = out.replace("$basearch", arch)
    out = out.replace("$arch", arch)
    if releasever is not None:
        out = out.replace("$releasever", releasever)
    elif "$releasever" in out:
        # The plugin's pytest_configure should have caught this earlier;
        # this check is a belt-and-suspenders.
        raise RepodataFetchError(
            f"URL {url!r} contains $releasever but no --releasever was provided"
        )
    if not out.endswith("/"):
        out += "/"
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


_USER_AGENT = "azl-repo-tests/0.1"
_FETCH_TIMEOUT_SECS = 60

# Retry policy for transient network errors. We retry only on errors
# that *might* be transient (URLError, OSError, 5xx HTTPError); 4xx
# responses and SSL errors are not retried.
_HTTP_MAX_ATTEMPTS = 3
_HTTP_BACKOFF_BASE_SECS = 1.0  # 1s, 2s, 4s with exponential growth.


def _http_get_once(url: str, dest_dir: Path, dest_basename: str) -> Path:
    """Single-shot fetch of *url* to a uniquely-named temp file in *dest_dir*.

    Returns the path of the (still-named) temp file on success — the
    caller is responsible for the atomic ``replace`` to the final
    destination once the body is fully written.

    The temp file uses a per-call unique name (via
    :func:`tempfile.mkstemp`) so two parallel xdist workers fetching
    the same URL never share a partial-write file. ``mkstemp``
    additionally creates the file with mode ``0600`` and an exclusive
    open, removing the TOCTOU race that a fixed ``.part`` filename has.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    fd, tmp_path = tempfile.mkstemp(
        dir=str(dest_dir), prefix=dest_basename + ".", suffix=".part"
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "wb") as fh:
            with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECS) as resp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
    except BaseException:
        # Best-effort cleanup; suppress secondary errors so we don't
        # mask the original.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("failed to clean up partial fetch %s", tmp, exc_info=True)
        raise
    return tmp


def _http_get(url: str, dest: Path) -> None:
    """Download *url* to *dest* (streaming), with retries on transient errors.

    Atomicity: the body is written to a unique tempfile in the same
    directory and ``Path.replace``-d into place on success. Concurrent
    xdist workers downloading the same URL will each succeed with their
    own tempfile; the final ``replace`` is atomic on POSIX so the worst
    case is a redundant download, never a torn file.

    Retries: up to :data:`_HTTP_MAX_ATTEMPTS` attempts with exponential
    backoff. We retry on:

    * :class:`urllib.error.URLError` (DNS failures, connection resets)
    * :class:`OSError` (low-level socket errors)
    * :class:`urllib.error.HTTPError` with ``code >= 500`` (server-side)
    * :class:`http.client.HTTPException` — covers
      :class:`~http.client.IncompleteRead` (server reset after sending
      a partial body — the most common transient failure for the
      multi-MB filelists download) and
      :class:`~http.client.RemoteDisconnected`. These are not
      :class:`OSError`/:class:`URLError` subclasses, so without an
      explicit catch they would bubble unretried even though they're
      exactly the kind of transient mid-stream failure the retry loop
      is designed to absorb.

    We do **not** retry on 4xx responses (likely permanent — wrong URL,
    auth, etc.) or on SSL/cert errors (likewise permanent and worth
    failing fast on).
    """
    last_exc: Exception | None = None
    for attempt in range(1, _HTTP_MAX_ATTEMPTS + 1):
        try:
            tmp = _http_get_once(url, dest.parent, dest.name)
        except urllib.error.HTTPError as exc:
            if exc.code is not None and exc.code < 500:
                # Permanent error; don't retry.
                raise RepodataFetchError(
                    f"failed to fetch {url}: HTTP {exc.code} {exc.reason}"
                ) from exc
            last_exc = exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            last_exc = exc
        else:
            try:
                tmp.replace(dest)
            except OSError as exc:
                # Move-into-place failures are not retryable.
                raise RepodataFetchError(
                    f"failed to move {tmp} -> {dest}: {exc}"
                ) from exc
            return

        if attempt < _HTTP_MAX_ATTEMPTS:
            delay = _HTTP_BACKOFF_BASE_SECS * (2 ** (attempt - 1))
            logger.warning(
                "fetch %s failed (attempt %d/%d): %s — retrying in %.1fs",
                url, attempt, _HTTP_MAX_ATTEMPTS, last_exc, delay,
            )
            time.sleep(delay)

    raise RepodataFetchError(
        f"failed to fetch {url} after {_HTTP_MAX_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def _verify_sha(path: Path, algo: str, expected: str) -> None:
    """Raise :class:`RepodataFetchError` if the file's hash does not match."""
    try:
        h = hashlib.new(algo)
    except ValueError as exc:
        raise RepodataFetchError(
            f"unsupported checksum algorithm {algo!r}"
        ) from exc
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual.lower() != expected.lower():
        raise RepodataFetchError(
            f"checksum mismatch for {path}: expected {algo}:{expected}, got {actual}"
        )


# ---------------------------------------------------------------------------
# repomd.xml parsing
# ---------------------------------------------------------------------------

_REPOMD_NS = "{http://linux.duke.edu/metadata/repo}"


@dataclass(frozen=True)
class _RepomdRecord:
    """A single ``<data type="...">`` record in repomd.xml."""

    type: str
    location: str
    checksum_algo: str | None
    checksum: str | None


def _parse_repomd(repomd_path: Path) -> list[_RepomdRecord]:
    try:
        tree = _parse_safe(repomd_path)
    except ET.ParseError as exc:
        raise RepodataParseError(f"failed to parse {repomd_path}: {exc}") from exc

    records: list[_RepomdRecord] = []
    root = tree.getroot()
    for data in root.findall(f"{_REPOMD_NS}data"):
        type_ = data.get("type") or ""
        loc = data.find(f"{_REPOMD_NS}location")
        checksum = data.find(f"{_REPOMD_NS}checksum")
        if loc is None:
            continue
        href = loc.get("href")
        if not href:
            continue
        records.append(
            _RepomdRecord(
                type=type_,
                location=href,
                checksum_algo=(checksum.get("type") if checksum is not None else None),
                checksum=(checksum.text if checksum is not None else None),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Choosing primary / filelists records
# ---------------------------------------------------------------------------

# Order of preference (most desirable first). _zck records are skipped
# at the location level: zck (zchunk) is a partial-update format we
# don't support — even with zstd available, the chunk framing is a
# distinct envelope.
_PRIMARY_TYPES_PREFERRED = ("primary", "primary_xml", "primary_zck")
_FILELISTS_TYPES_PREFERRED = ("filelists", "filelists_xml", "filelists_zck")

# Compression suffixes we know how to decode. ``""`` represents an
# uncompressed file (e.g., ``primary.xml``).
_DECODABLE_SUFFIXES: tuple[str, ...] = ("", ".gz", ".xz", ".zst")


def _can_decode(href: str) -> bool:
    lower = href.lower()
    if lower.endswith(".zck"):
        return False
    for suf in _DECODABLE_SUFFIXES:
        if suf == "":
            continue
        if lower.endswith(suf):
            return True
    # No recognizable compression suffix — assume uncompressed XML.
    return lower.endswith(".xml")


def _pick_record(
    records: Iterable[_RepomdRecord], preferred_types: tuple[str, ...]
) -> _RepomdRecord:
    by_type: dict[str, _RepomdRecord] = {}
    for r in records:
        # Keep the first record of each type; primary_xml + primary
        # both map to the canonical primary metadata.
        by_type.setdefault(r.type, r)
    for ptype in preferred_types:
        if ptype not in by_type:
            continue
        rec = by_type[ptype]
        if not _can_decode(rec.location):
            continue
        return rec
    available = sorted({r.type for r in records})
    raise RepodataFetchError(
        f"repomd has no decodable record among {preferred_types}; "
        f"types found: {available}"
    )


# ---------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------


def _open_decompressed(path: Path):
    """Open *path*, transparently decompressing based on suffix.

    Supports ``.gz``, ``.xz``, and ``.zst``. ``.zst`` requires either
    Python 3.14's stdlib ``compression.zstd`` or the third-party
    ``zstandard`` package; we try both.
    """
    name = path.name.lower()
    if name.endswith(".gz"):
        return gzip.open(path, "rb")
    if name.endswith(".xz"):
        return lzma.open(path, "rb")
    if name.endswith(".zst"):
        return _open_zstd(path)
    return open(path, "rb")


def _open_zstd(path: Path):
    """Open a zstd-compressed file for streaming reads."""
    try:
        from compression import zstd as _stdlib_zstd  # type: ignore[attr-defined]
    except ImportError:
        _stdlib_zstd = None  # type: ignore[assignment]
    if _stdlib_zstd is not None:
        return _stdlib_zstd.open(path, "rb")
    try:
        import zstandard  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover
        raise RepodataFetchError(
            f"cannot decompress {path}: this Python lacks compression.zstd "
            "(<3.14) and the 'zstandard' package is not installed. "
            "Install zstandard, or use a Python 3.14+ runtime."
        ) from exc
    fh = open(path, "rb")
    dctx = zstandard.ZstdDecompressor()
    return dctx.stream_reader(fh)


# ---------------------------------------------------------------------------
# primary.xml streaming parse
# ---------------------------------------------------------------------------

_PRIMARY_NS = "{http://linux.duke.edu/metadata/common}"
_RPM_NS = "{http://linux.duke.edu/metadata/rpm}"


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    return el.text


def _parse_primary(path: Path) -> Generator[Package, None, None]:
    """Stream-parse ``primary.xml(.gz|.xz)`` and yield :class:`Package` records.

    Uses the standard iterparse pattern of subscribing to ``start``
    events to capture the root element, then ``end`` events to consume
    yielded ``<package>`` subtrees. After each yielded element we
    ``elem.clear()`` AND ``root.remove(elem)`` so that the parent's
    child-list does not accumulate every package's worth of memory
    over the lifetime of the stream — without the ``remove`` call,
    only the *children* of each cleared package element are freed,
    leaving an ever-growing list of empty placeholder packages
    attached to the root. For Azure Linux's primary.xml that's
    measurable but tolerable; for filelists.xml it can be hundreds
    of MB.
    """
    with _open_decompressed(path) as fh:
        try:
            iterator = _iterparse_safe(fh, events=("start", "end"))
            root: ET.Element | None = None
            for event, elem in iterator:
                if event == "start":
                    if root is None:
                        root = elem
                    continue
                # event == "end"
                if elem.tag != f"{_PRIMARY_NS}package":
                    continue

                name = _text(elem.find(f"{_PRIMARY_NS}name")) or ""
                arch = _text(elem.find(f"{_PRIMARY_NS}arch")) or ""
                version_el = elem.find(f"{_PRIMARY_NS}version")
                if version_el is None or not name or not arch:
                    elem.clear()
                    if root is not None:
                        root.remove(elem)
                    continue
                epoch = int(version_el.get("epoch") or "0")
                ver = version_el.get("ver") or ""
                rel = version_el.get("rel") or ""

                fmt = elem.find(f"{_PRIMARY_NS}format")
                vendor: str | None = None
                sourcerpm: str | None = None
                provides: list[str] = []
                conflicts: list[ConflictEntry] = []
                files: list[FileEntry] = []
                if fmt is not None:
                    sourcerpm = _text(fmt.find(f"{_RPM_NS}sourcerpm"))
                    vendor = _text(fmt.find(f"{_RPM_NS}vendor"))
                    pr = fmt.find(f"{_RPM_NS}provides")
                    if pr is not None:
                        for entry in pr.findall(f"{_RPM_NS}entry"):
                            n = entry.get("name")
                            if n:
                                provides.append(n)
                    cf = fmt.find(f"{_RPM_NS}conflicts")
                    if cf is not None:
                        for entry in cf.findall(f"{_RPM_NS}entry"):
                            n = entry.get("name")
                            if not n:
                                continue
                            # Capture flags + EVR so consumers can tell
                            # bare ``Conflicts: foo`` apart from a
                            # versioned ``Conflicts: foo < 1.0``. The
                            # cross-repo file-conflicts test treats
                            # only the bare form as truly suppressing
                            # a file overlap; versioned conflicts are
                            # surfaced as needing-verification because
                            # they may not actually cover the
                            # observed package version.
                            flags_attr = entry.get("flags")
                            ep = entry.get("epoch")
                            try:
                                ep_int = int(ep) if ep is not None else None
                            except ValueError:
                                ep_int = None
                            conflicts.append(
                                ConflictEntry(
                                    name=n,
                                    flags=flags_attr or None,
                                    epoch=ep_int,
                                    version=entry.get("ver") or None,
                                    release=entry.get("rel") or None,
                                )
                            )
                    for fent in fmt.findall(f"{_PRIMARY_NS}file"):
                        if not fent.text:
                            continue
                        ftype = fent.get("type")
                        files.append(
                            FileEntry(
                                path=fent.text,
                                is_directory=(ftype == "dir"),
                                is_ghost=(ftype == "ghost"),
                            )
                        )
                # Vendor in a separate top-level element on some repos
                if vendor is None:
                    vendor = _text(elem.find(f"{_PRIMARY_NS}vendor"))

                # RPM auto-emits ``Provides: <name> = <epoch>:<ver>-<rel>``.
                # Most createrepo runs include it explicitly, but ensure
                # the bare ``<name>`` is present so virtual-conflict
                # matching by name works even if the explicit provide is
                # absent for some reason.
                if name not in provides:
                    provides.append(name)

                yield Package(
                    nevra=NEVRA(
                        name=name,
                        epoch=epoch,
                        version=ver,
                        release=rel,
                        arch=arch,
                    ),
                    vendor=vendor,
                    sourcerpm=sourcerpm,
                    summary=_text(elem.find(f"{_PRIMARY_NS}summary")),
                    provides=provides,
                    conflicts=conflicts,
                    files=files,
                )

                elem.clear()
                if root is not None:
                    root.remove(elem)
        except ET.ParseError as exc:
            raise RepodataParseError(f"failed to parse {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# filelists.xml streaming parse
# ---------------------------------------------------------------------------

_FILELISTS_NS = "{http://linux.duke.edu/metadata/filelists}"


@dataclass(frozen=True)
class _FilelistEntry:
    """One ``(NEVRA, file)`` entry from filelists.xml."""

    nevra: NEVRA
    path: str
    is_directory: bool
    is_ghost: bool


def _parse_filelists(path: Path) -> Generator[_FilelistEntry, None, None]:
    """Stream-parse filelists.xml(.gz|.xz). Yields one entry per file/dir.

    See :func:`_parse_primary` for the rationale behind subscribing to
    ``start`` events as well: it lets us hold a reference to the root
    so we can ``root.remove(elem)`` after each ``<package>`` is
    consumed, which is what actually keeps the parser's working-set
    memory bounded for very large filelists.
    """
    with _open_decompressed(path) as fh:
        try:
            iterator = _iterparse_safe(fh, events=("start", "end"))
            root: ET.Element | None = None
            for event, elem in iterator:
                if event == "start":
                    if root is None:
                        root = elem
                    continue
                if elem.tag != f"{_FILELISTS_NS}package":
                    continue
                name = elem.get("name") or ""
                arch = elem.get("arch") or ""
                ver_el = elem.find(f"{_FILELISTS_NS}version")
                if ver_el is None or not name or not arch:
                    elem.clear()
                    if root is not None:
                        root.remove(elem)
                    continue
                nevra = NEVRA(
                    name=name,
                    epoch=int(ver_el.get("epoch") or "0"),
                    version=ver_el.get("ver") or "",
                    release=ver_el.get("rel") or "",
                    arch=arch,
                )
                for fent in elem.findall(f"{_FILELISTS_NS}file"):
                    if not fent.text:
                        continue
                    ftype = fent.get("type")
                    yield _FilelistEntry(
                        nevra=nevra,
                        path=fent.text,
                        is_directory=(ftype == "dir"),
                        is_ghost=(ftype == "ghost"),
                    )
                elem.clear()
                if root is not None:
                    root.remove(elem)
        except ET.ParseError as exc:
            raise RepodataParseError(f"failed to parse {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


@dataclass
class RepodataLoader:
    """Lazy loader for one repo at one (arch, releasever) combination.

    Caches all downloaded artifacts under ``cache_dir`` (which the
    caller is expected to make unique per ``(repo, arch, releasever)``).
    """

    base_url_substituted: str
    cache_dir: Path
    _records_cache: list[_RepomdRecord] | None = None

    def _local_path_for(self, href: str) -> Path:
        # Mirror the relative href under cache_dir so two records with
        # the same basename in different subdirs don't collide.
        rel = href.lstrip("/")
        # Don't escape cache_dir — strip any "../" components.
        parts = [p for p in Path(rel).parts if p not in ("", "..")]
        return self.cache_dir.joinpath(*parts)

    def _download(self, href: str, expected_algo: str | None, expected_hash: str | None) -> Path:
        local = self._local_path_for(href)
        if local.exists():
            # If we have a checksum, verify cached file; else trust it.
            if expected_algo and expected_hash:
                try:
                    _verify_sha(local, expected_algo, expected_hash)
                    return local
                except RepodataFetchError:
                    logger.debug("Cached %s failed checksum, re-downloading", local)
                    local.unlink(missing_ok=True)
            else:
                return local
        url = urllib.parse.urljoin(self.base_url_substituted, href)
        logger.debug("Fetching %s", url)
        _http_get(url, local)
        if expected_algo and expected_hash:
            try:
                _verify_sha(local, expected_algo, expected_hash)
            except RepodataFetchError:
                local.unlink(missing_ok=True)
                raise
        return local

    def _repomd_records(self) -> list[_RepomdRecord]:
        if self._records_cache is not None:
            return self._records_cache
        repomd_url = urllib.parse.urljoin(
            self.base_url_substituted, "repodata/repomd.xml"
        )
        repomd_path = self.cache_dir / "repodata" / "repomd.xml"
        # Always re-download repomd (it's small and is the source of
        # truth for what other artifacts to fetch).
        logger.debug("Fetching %s", repomd_url)
        _http_get(repomd_url, repomd_path)
        records = _parse_repomd(repomd_path)
        self._records_cache = records
        return records

    def packages(self) -> Generator[Package, None, None]:
        """Yield every package described by ``primary.xml``."""
        records = self._repomd_records()
        rec = _pick_record(records, _PRIMARY_TYPES_PREFERRED)
        path = self._download(rec.location, rec.checksum_algo, rec.checksum)
        yield from _parse_primary(path)

    def filelist_entries(self) -> Generator[_FilelistEntry, None, None]:
        """Yield every ``(NEVRA, file)`` entry from ``filelists.xml``."""
        records = self._repomd_records()
        rec = _pick_record(records, _FILELISTS_TYPES_PREFERRED)
        path = self._download(rec.location, rec.checksum_algo, rec.checksum)
        yield from _parse_filelists(path)
