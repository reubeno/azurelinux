#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""audit-repos -- audit RPM repo sync discrepancies for Azure Linux 4.

Three hard-coded comparison modes (subcommands):

  koji-vs-blob   source = koji dist-repo (azl4/latest),
                 dest   = blob-storage repos (standard AZL layout)
  blob-vs-pmc    source = blob-storage repos,
                 dest   = packages.microsoft.com beta repos
  koji-vs-pmc    source = koji dist-repo,
                 dest   = packages.microsoft.com beta repos

Pairing is asymmetric: the *source* side is authoritative; the *dest*
side is what we audit. Per (kind, arch) universe (main / debuginfo /
srpms; x86_64 / aarch64 / src) the script reports four flat lists:

  must-add             NEVR present in source but absent from dest
  must-remove          Every NEVR in dest whose *name* does not appear
                       in source at all
  unexpected-versions  NEVR in dest whose EVR is strictly greater than
                       the maximum EVR of that (name, arch) in source
  misrouted            NEVR in dest whose actual base/sdk sub-repo
                       disagrees with the channel reported by
                       `azldev package list --rpm-file ...`
                       (skipped if --no-routing-audit is set)

Stale older NEVRs in dest (EVR strictly less than source's max for the
same (name, arch), but the NEVR itself absent from source) are silently
ignored: they are not must-remove (the name is in source) and they are
not unexpected (older, not newer).

Per-arch comparison only: x86_64 is never compared against aarch64.

Routing audit inspects only dest packages that are NOT must-be-removed.
An empty publishChannel from azldev means "no opinion" and is not
flagged. azldev handles its own sibling-channel inheritance; this script
trusts its answer verbatim.

The script always forces fresh repodata downloads (no on-disk cache).

Dependencies (all installable from system / pip):
  * python3-createrepo_c   (Fedora/AZL package)
  * python3-rpm            (Fedora/AZL package)
  * `azldev` on PATH       (only when routing audit is enabled)
"""

from __future__ import annotations

import argparse
import enum
import json
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import createrepo_c as cr
import rpm

# ---------------------------------------------------------------------------
# Hard-coded constants -- see module docstring for rationale.
# ---------------------------------------------------------------------------

PROG = Path(sys.argv[0]).name

KOJI_URL = "https://20.225.0.246/kojifiles/repos-dist/azl4/latest"
BLOB_URL = "https://stcontroltowerdevjwisitg.blob.core.windows.net/azl4-dev"
PMC_URL = "https://packages.microsoft.com/azurelinux/4.0/beta"

ARCHES: tuple[str, ...] = ("x86_64", "aarch64")
SRPM_ARCH = "src"

KIND_MAIN = "main"
KIND_DEBUGINFO = "debuginfo"
KIND_SRPMS = "srpms"

CHANNEL_BASE = "base"
CHANNEL_SDK = "sdk"
ALLOWED_CHANNELS = (CHANNEL_BASE, CHANNEL_SDK)

# azldev publishChannel is `rpm-<channel>[-<kind-suffix>]`. We strip
# the kind suffix because base/sdk routing applies uniformly across
# main / debuginfo / srpms sub-repos of the same channel.
AZLDEV_CHANNEL_PREFIX = "rpm-"
AZLDEV_CHANNEL_KIND_SUFFIXES: tuple[str, ...] = ("-srpm", "-debuginfo")

# HTTP knobs.
HTTP_TIMEOUT = 60.0
HTTP_RETRIES = 4
HTTP_BACKOFF_BASE = 1.5
USER_AGENT = "audit-repos/1.0"


# ---------------------------------------------------------------------------
# Tiny logging helpers (stderr; stdout is reserved for the report).
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    sys.stderr.write(f"{PROG}: {msg}\n")


def warn(msg: str) -> None:
    sys.stderr.write(f"{PROG}: warning: {msg}\n")


def die(msg: str, *, code: int = 2) -> None:
    sys.stderr.write(f"{PROG}: {msg}\n")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Layouts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubrepoLoc:
    """One concrete sub-repo location under a layout prefix.

    For the AZL standard layout (blob/pmc) ``channel`` is ``base`` or
    ``sdk``. For the koji dist-repo layout there is no base/sdk split,
    so ``channel`` is None.
    """

    kind: str             # KIND_MAIN | KIND_DEBUGINFO | KIND_SRPMS
    arch: str             # x86_64 | aarch64 | src
    channel: str | None   # base | sdk | None (for koji)
    subpath: str          # path relative to the layout prefix

    def label(self) -> str:
        ch = self.channel or "-"
        return f"{ch}/{self.kind}/{self.arch}"


def koji_subrepos() -> list[SubrepoLoc]:
    """Koji dist-repo layout: ``$basearch``, ``$basearch/debug``, ``src``."""
    out: list[SubrepoLoc] = []
    for a in ARCHES:
        out.append(SubrepoLoc(KIND_MAIN, a, None, a))
        out.append(SubrepoLoc(KIND_DEBUGINFO, a, None, f"{a}/debug"))
    out.append(SubrepoLoc(KIND_SRPMS, SRPM_ARCH, None, "src"))
    return out


def azl_subrepos() -> list[SubrepoLoc]:
    """Standard AZL layout: base/sdk x main/debuginfo/srpms x arch."""
    out: list[SubrepoLoc] = []
    for ch in ALLOWED_CHANNELS:
        for a in ARCHES:
            out.append(SubrepoLoc(KIND_MAIN, a, ch, f"{ch}/{a}"))
            out.append(SubrepoLoc(KIND_DEBUGINFO, a, ch, f"{ch}/debuginfo/{a}"))
        out.append(SubrepoLoc(KIND_SRPMS, SRPM_ARCH, ch, f"{ch}/srpms"))
    return out


class LayoutKind(str, enum.Enum):
    KOJI = "koji"
    AZL = "azl"


def subrepos_for(layout: LayoutKind) -> list[SubrepoLoc]:
    return koji_subrepos() if layout is LayoutKind.KOJI else azl_subrepos()


# ---------------------------------------------------------------------------
# Package row
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PkgRow:
    """One physical RPM row parsed from a primary.xml.

    ``channel`` is the sub-repo channel the row was loaded from (base /
    sdk / None for koji). ``location_href`` is relative to the
    sub-repo's URL.
    """

    name: str
    epoch: str
    version: str
    release: str
    arch: str
    kind: str                # main | debuginfo | srpms
    channel: str | None      # base | sdk | None
    location_href: str
    sourcerpm: str           # the bare `<sourcerpm>` value, possibly ""
    source_name: str         # parsed source-package name (== name for srpms)
    subrepo_url: str         # absolute URL of the sub-repo this row came from

    @property
    def evr(self) -> tuple[str, str, str]:
        return (self.epoch, self.version, self.release)

    @property
    def evr_str(self) -> str:
        if self.epoch in ("", "0"):
            return f"{self.version}-{self.release}"
        return f"{self.epoch}:{self.version}-{self.release}"

    @property
    def nevr(self) -> str:
        return f"{self.name}-{self.evr_str}"

    @property
    def nevra(self) -> str:
        return f"{self.nevr}.{self.arch}"

    @property
    def url(self) -> str:
        return _join_url(self.subrepo_url, self.location_href)


@dataclass(frozen=True)
class NEVR:
    """Per-comparison identity (arch carried separately at the bucket level)."""

    name: str
    epoch: str
    version: str
    release: str

    @property
    def evr(self) -> tuple[str, str, str]:
        return (self.epoch, self.version, self.release)

    @property
    def evr_str(self) -> str:
        if self.epoch in ("", "0"):
            return f"{self.version}-{self.release}"
        return f"{self.epoch}:{self.version}-{self.release}"

    @property
    def str_(self) -> str:
        return f"{self.name}-{self.evr_str}"


def _nevr_of(r: PkgRow) -> NEVR:
    return NEVR(r.name, r.epoch, r.version, r.release)


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def _normalize_prefix(p: str) -> str:
    return p.rstrip("/")


def _join_url(prefix: str, rel: str) -> str:
    return urllib.parse.urljoin(prefix.rstrip("/") + "/", rel.lstrip("/"))


# ---------------------------------------------------------------------------
# HTTP fetch (force-fresh; no caching across runs)
# ---------------------------------------------------------------------------

def _build_ssl_context(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _http_get(
    url: str, dest: Path, ssl_context: ssl.SSLContext | None,
) -> None:
    """Download *url* to *dest* with bounded retry.

    Retries on transient transport errors and HTTP 5xx. Bails on 4xx.
    Cache-Control headers explicitly force a fresh fetch through any
    intermediary cache.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )
    last_exc: BaseException | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(
                req, timeout=HTTP_TIMEOUT, context=ssl_context,
            ) as resp, open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            return
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and attempt < HTTP_RETRIES - 1:
                last_exc = e
                log(f"    HTTP {e.code} fetching {url}; retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, FileNotFoundError):
                raise
            if attempt < HTTP_RETRIES - 1:
                last_exc = e
                log(f"    URL error fetching {url} ({e.reason}); retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
        except (TimeoutError, OSError) as e:
            if attempt < HTTP_RETRIES - 1:
                last_exc = e
                log(f"    transport error fetching {url} ({e}); retrying")
                time.sleep(HTTP_BACKOFF_BASE * (2 ** attempt))
                continue
            raise
    if last_exc is not None:
        raise last_exc


_FETCH_OK = "ok"
_FETCH_MISSING = "missing"


def _fetch_primary(
    subrepo_url: str, scratch_dir: Path,
    ssl_context: ssl.SSLContext | None,
) -> tuple[str, Path | None]:
    """Fetch ``repodata/repomd.xml`` + the ``primary`` record.

    Returns ``(_FETCH_OK, primary_path)`` on success, or
    ``(_FETCH_MISSING, None)`` when ``repomd.xml`` is absent (404).
    """
    repodata_dir = scratch_dir / "repodata"
    repodata_dir.mkdir(parents=True, exist_ok=True)

    repomd_url = _join_url(subrepo_url, "repodata/repomd.xml")
    repomd_path = repodata_dir / "repomd.xml"
    try:
        _http_get(repomd_url, repomd_path, ssl_context)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _FETCH_MISSING, None
        raise
    except urllib.error.URLError as e:
        if isinstance(e.reason, FileNotFoundError):
            return _FETCH_MISSING, None
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

    safe_rel = primary_href.lstrip("/")
    if ".." in Path(safe_rel).parts:
        raise RuntimeError(
            f"refusing to write metadata record outside scratch: {primary_href!r}"
        )
    primary_url = _join_url(subrepo_url, primary_href)
    primary_path = scratch_dir / safe_rel
    _http_get(primary_url, primary_path, ssl_context)
    return _FETCH_OK, primary_path


# ---------------------------------------------------------------------------
# Primary.xml parsing
# ---------------------------------------------------------------------------

def _strip_srpm_suffix(rpm_sourcerpm: str | None) -> str:
    """Extract the source-package *name* from a ``<sourcerpm>`` value.

    E.g. ``bash-5.2.21-1.azl4.src.rpm`` -> ``bash``.
    """
    if not rpm_sourcerpm:
        return ""
    s = rpm_sourcerpm
    if s.endswith(".src.rpm"):
        s = s[: -len(".src.rpm")]
    parts = s.rsplit("-", 2)
    if len(parts) >= 3:
        return parts[0]
    return s


def _load_rows(
    primary_path: Path, sub: SubrepoLoc, subrepo_url: str,
) -> list[PkgRow]:
    """Parse *primary_path* into ``PkgRow``s, annotated with sub-repo info."""
    seen: set[tuple] = set()
    out: list[PkgRow] = []

    def cb(pkg) -> None:
        if not pkg.name:
            return
        if sub.kind == KIND_SRPMS:
            source_name = pkg.name
        else:
            source_name = _strip_srpm_suffix(pkg.rpm_sourcerpm) or pkg.name
        row = PkgRow(
            name=pkg.name,
            epoch=(pkg.epoch or "0"),
            version=(pkg.version or ""),
            release=(pkg.release or ""),
            arch=(pkg.arch or ""),
            kind=sub.kind,
            channel=sub.channel,
            location_href=(pkg.location_href or ""),
            sourcerpm=(pkg.rpm_sourcerpm or ""),
            source_name=source_name,
            subrepo_url=subrepo_url,
        )
        key = (
            row.name, row.epoch, row.version, row.release, row.arch,
            row.channel, row.location_href,
        )
        if key in seen:
            return
        seen.add(key)
        out.append(row)

    cr.xml_parse_primary(
        str(primary_path), pkgcb=cb, do_files=False,
        warningcb=lambda *_: True,
    )
    return out


# ---------------------------------------------------------------------------
# Side loading
# ---------------------------------------------------------------------------

@dataclass
class SideRows:
    """All rows from one side (source or dest), tagged by layout."""

    prefix: str
    layout: LayoutKind
    rows: list[PkgRow]
    # Subrepo URLs that returned 404 (informational only -- this is
    # normal for layout slots that aren't published).
    missing_subrepos: list[str] = field(default_factory=list)


def _load_side(
    prefix: str, layout: LayoutKind, ssl_context: ssl.SSLContext | None,
    scratch_dir: Path, label: str,
) -> SideRows:
    log(f"{label} <- {prefix}  ({layout.value} layout)")
    rows: list[PkgRow] = []
    missing: list[str] = []
    for sub in subrepos_for(layout):
        url = _join_url(prefix, sub.subpath)
        slot_scratch = scratch_dir / label / sub.label().replace("/", "_")
        status, primary = _fetch_primary(url, slot_scratch, ssl_context)
        if status == _FETCH_MISSING:
            log(f"  {sub.label()}: absent ({url})")
            missing.append(url)
            continue
        loaded = _load_rows(primary, sub, url)
        log(f"  {sub.label()}: {len(loaded)} pkgs ({url})")
        rows.extend(loaded)
    return SideRows(prefix=prefix, layout=layout, rows=rows,
                    missing_subrepos=missing)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

@dataclass
class Findings:
    """All four discrepancy lists for one (kind, arch) universe.

    Each entry references concrete dest/source rows so the renderer can
    emit either NEVR or full URI form. ``must_add`` entries carry the
    *source* row (and a projected dest URL filled in later by the
    routing pass when available); the other three carry the dest row.
    """

    # source row (must-add has no dest row to point at).
    must_add: list[PkgRow] = field(default_factory=list)
    # dest row.
    must_remove: list[PkgRow] = field(default_factory=list)
    unexpected_versions: list[PkgRow] = field(default_factory=list)
    misrouted: list["MisroutedRow"] = field(default_factory=list)


@dataclass
class MisroutedRow:
    """A dest row whose actual channel disagrees with azldev's verdict."""

    row: PkgRow
    actual_channel: str            # base | sdk (row.channel)
    expected_channel: str          # base | sdk (from azldev)


def _bucket_by_kind_arch(rows: Iterable[PkgRow]) -> dict[tuple[str, str], list[PkgRow]]:
    out: dict[tuple[str, str], list[PkgRow]] = defaultdict(list)
    for r in rows:
        out[(r.kind, r.arch)].append(r)
    return out


def _max_evr_per_name(rows: Iterable[PkgRow]) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    for r in rows:
        cur = out.get(r.name)
        if cur is None or rpm.labelCompare(r.evr, cur) > 0:
            out[r.name] = r.evr
    return out


def _compare(src: SideRows, dst: SideRows) -> Findings:
    """Compare *src* vs *dst* and return must-add / must-remove / unexpected.

    Routing (misrouted) is populated separately by :func:`_routing_audit`.
    Comparison is strictly per (kind, arch). Stale older NEVRs in dest
    (EVR < source's max for the same (name, arch), NEVR absent from
    source) are ignored.
    """
    findings = Findings()

    src_by = _bucket_by_kind_arch(src.rows)
    dst_by = _bucket_by_kind_arch(dst.rows)

    for bucket in sorted(set(src_by) | set(dst_by)):
        s_rows = src_by.get(bucket, [])
        d_rows = dst_by.get(bucket, [])

        s_names = {r.name for r in s_rows}
        s_idents = {_nevr_of(r) for r in s_rows}
        d_idents_seen: dict[NEVR, list[PkgRow]] = defaultdict(list)
        for r in d_rows:
            d_idents_seen[_nevr_of(r)].append(r)

        # 1) MUST-REMOVE: dest rows whose name does not appear in source.
        for r in d_rows:
            if r.name not in s_names:
                findings.must_remove.append(r)

        # 2) MUST-ADD: source NEVRs absent from dest.
        d_idents = set(d_idents_seen.keys())
        for r in s_rows:
            nevr = _nevr_of(r)
            if nevr not in d_idents:
                findings.must_add.append(r)

        # 3) UNEXPECTED-VERSIONS: dest NEVR whose EVR is strictly newer
        #    than source's max EVR for that (name, arch). Names absent
        #    from source are already covered by must-remove and are
        #    skipped here to avoid double-reporting.
        src_max = _max_evr_per_name(s_rows)
        for nevr, rows in d_idents_seen.items():
            if nevr.name not in src_max:
                continue
            if rpm.labelCompare(nevr.evr, src_max[nevr.name]) > 0:
                findings.unexpected_versions.extend(rows)

    # Stable ordering for deterministic output.
    findings.must_add.sort(key=lambda r: (r.kind, r.arch, r.name, r.evr_str,
                                          r.channel or "", r.location_href))
    findings.must_remove.sort(key=lambda r: (r.kind, r.arch, r.name, r.evr_str,
                                             r.channel or "", r.location_href))
    findings.unexpected_versions.sort(
        key=lambda r: (r.kind, r.arch, r.name, r.evr_str,
                       r.channel or "", r.location_href))
    return findings


# ---------------------------------------------------------------------------
# Routing audit (azldev)
# ---------------------------------------------------------------------------

def _normalize_publish_channel(raw: str) -> str:
    """`rpm-<channel>[-<kind>]` -> `<channel>`. Empty -> empty.

    A value that doesn't start with the expected `rpm-` prefix is
    returned unchanged so the caller can flag it.
    """
    if not raw:
        return ""
    if not raw.startswith(AZLDEV_CHANNEL_PREFIX):
        return raw
    s = raw[len(AZLDEV_CHANNEL_PREFIX):]
    for suffix in AZLDEV_CHANNEL_KIND_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def _query_azldev_channels(
    rows: Iterable[PkgRow], scratch_dir: Path, repo_root: Path,
    label: str,
) -> dict[tuple[str, str], str] | None:
    """Query azldev `package list --rpm-file` for every (name, source_name)
    pair in *rows* and return ``{(packageName, type): channel}``.

    Returns ``None`` if azldev fails (caller falls back gracefully).
    *type* is azldev's ``rpm`` / ``srpm`` discriminator. *channel* is
    the normalised ``base`` / ``sdk`` value; entries with empty or
    unrecognised publishChannel are omitted from the result.
    """
    pairs: set[tuple[str, str]] = set()
    for r in rows:
        pairs.add((r.name, r.source_name))
    if not pairs:
        return {}

    map_path = scratch_dir / f"rpm_source_map.{label}.json"
    map_path.write_text(json.dumps(
        sorted(
            ({"packageName": pn, "sourcePackageName": sn} for pn, sn in pairs),
            key=lambda d: (d["packageName"], d["sourcePackageName"]),
        ),
        indent=2,
    ))
    log(f"  azldev[{label}]: invoking with {len(pairs)} (pkg, srpm) pair(s)")
    proc = subprocess.run(
        ["azldev", "package", "list", "--rpm-file", str(map_path),
         "-q", "-O", "json"],
        capture_output=True, text=True, cwd=repo_root, check=False,
    )
    if proc.returncode != 0:
        warn(f"azldev failed for {label}; downstream routing data will be unavailable")
        sys.stderr.write(proc.stderr)
        return None
    rows_out = json.loads(proc.stdout)
    out: dict[tuple[str, str], str] = {}
    for row in rows_out:
        name = row.get("packageName", "") or ""
        rtype = row.get("type", "") or ""
        ch = _normalize_publish_channel(row.get("publishChannel", "") or "")
        if not name:
            continue
        if ch and ch not in ALLOWED_CHANNELS:
            warn(
                f"azldev returned unrecognised channel {ch!r} for "
                f"{name} ({rtype}); ignoring"
            )
            continue
        if ch:
            out[(name, rtype)] = ch
    return out


def _compute_misrouted(
    dst: SideRows,
    must_remove_rows: list[PkgRow],
    dest_channel_map: dict[tuple[str, str], str],
) -> list[MisroutedRow]:
    """Cross-reference each dest row's actual channel against the
    azldev-derived expected channel, skipping rows already in
    must-remove. Rows without an azldev opinion are silently kept.
    """
    removable = {
        (r.name, r.epoch, r.version, r.release, r.arch,
         r.channel, r.location_href)
        for r in must_remove_rows
    }
    out: list[MisroutedRow] = []
    for r in dst.rows:
        key = (r.name, r.epoch, r.version, r.release, r.arch,
               r.channel, r.location_href)
        if key in removable:
            continue
        azldev_type = "srpm" if r.kind == KIND_SRPMS else "rpm"
        expected = dest_channel_map.get((r.name, azldev_type))
        if not expected:
            continue
        if r.channel != expected:
            out.append(MisroutedRow(
                row=r, actual_channel=r.channel or "", expected_channel=expected,
            ))
    out.sort(key=lambda m: (m.row.kind, m.row.arch, m.row.name,
                            m.row.evr_str, m.actual_channel))
    return out


# ---------------------------------------------------------------------------
# Projected dest URLs (for MUST-ADD --show uri)
# ---------------------------------------------------------------------------

def _dest_layout_subpath(channel: str, sub_kind: str, arch: str) -> str:
    """Reproduce the AZL standard layout subpath for a (channel, kind, arch).

    Only used when the dest is an AZL-layout repo (blob or pmc); the
    koji layout is never a dest in any of the three modes.
    """
    if sub_kind == KIND_SRPMS:
        return f"{channel}/srpms"
    if sub_kind == KIND_DEBUGINFO:
        return f"{channel}/debuginfo/{arch}"
    return f"{channel}/{arch}"


def _project_dest_url(
    src_row: PkgRow, dest_prefix: str,
    azldev_channel_by_name: dict[tuple[str, str], str] | None,
) -> str | None:
    """Project the URL where *src_row* would land in the dest layout.

    Returns None when routing info isn't available (e.g. routing audit
    disabled, or azldev had no opinion for this package). The dest is
    always AZL-layout per the three supported modes.
    """
    if azldev_channel_by_name is None:
        return None
    azldev_type = "srpm" if src_row.kind == KIND_SRPMS else "rpm"
    channel = azldev_channel_by_name.get((src_row.name, azldev_type))
    if not channel:
        return None
    sub = _dest_layout_subpath(channel, src_row.kind, src_row.arch)
    rpm_basename = Path(src_row.location_href).name
    return _join_url(_join_url(dest_prefix, sub), f"Packages/{rpm_basename}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class ShowKey(str, enum.Enum):
    NEVR = "nevr"
    URI = "uri"


def _render_text(
    out: "object", findings: Findings, *, show: ShowKey, dest_prefix: str,
    azldev_channel_by_name: dict[tuple[str, str], str] | None,
) -> None:
    write = out.write

    def header(label: str, n: int) -> None:
        write(f"\n=== {label} ({n}) ===\n")

    header("MUST-ADD", len(findings.must_add))
    for r in findings.must_add:
        if show is ShowKey.NEVR:
            write(f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}\n")
        else:
            dest_url = _project_dest_url(r, dest_prefix, azldev_channel_by_name)
            dest_disp = dest_url if dest_url else "<dest URL unknown: routing info unavailable>"
            write(
                f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}\n"
                f"      src:  {r.url}\n"
                f"      dest: {dest_disp}\n"
            )

    header("MUST-REMOVE", len(findings.must_remove))
    for r in findings.must_remove:
        if show is ShowKey.NEVR:
            ch = f" [{r.channel}]" if r.channel else ""
            write(f"  [{r.kind}/{r.arch}]{ch} {r.nevr}.{r.arch}\n")
        else:
            write(
                f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}\n"
                f"      dest: {r.url}\n"
            )

    header("UNEXPECTED-VERSIONS", len(findings.unexpected_versions))
    for r in findings.unexpected_versions:
        if show is ShowKey.NEVR:
            ch = f" [{r.channel}]" if r.channel else ""
            write(f"  [{r.kind}/{r.arch}]{ch} {r.nevr}.{r.arch}\n")
        else:
            write(
                f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}\n"
                f"      dest: {r.url}\n"
            )

    header("MISROUTED", len(findings.misrouted))
    for m in findings.misrouted:
        r = m.row
        if show is ShowKey.NEVR:
            write(
                f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}  "
                f"(in {m.actual_channel}, expected {m.expected_channel})\n"
            )
        else:
            write(
                f"  [{r.kind}/{r.arch}] {r.nevr}.{r.arch}  "
                f"(in {m.actual_channel}, expected {m.expected_channel})\n"
                f"      dest: {r.url}\n"
            )

    write(
        f"\nsummary: {len(findings.must_add)} must-add, "
        f"{len(findings.must_remove)} must-remove, "
        f"{len(findings.unexpected_versions)} unexpected-versions, "
        f"{len(findings.misrouted)} misrouted\n"
    )


def _render_json(
    out: "object", findings: Findings, *, show: ShowKey, dest_prefix: str,
    azldev_channel_by_name: dict[tuple[str, str], str] | None,
) -> None:
    """JSON renderer. With ``show=URI`` includes URL fields; with ``show=NEVR``
    omits them (mirrors the text-form distinction).
    """
    include_urls = show is ShowKey.URI

    def src_entry(r: PkgRow) -> dict:
        entry = {
            "kind": r.kind, "arch": r.arch, "name": r.name,
            "epoch": r.epoch, "version": r.version, "release": r.release,
            "nevr": r.nevr, "nevra": r.nevra,
        }
        if include_urls:
            entry["src_url"] = r.url
            entry["projected_dest_url"] = _project_dest_url(
                r, dest_prefix, azldev_channel_by_name,
            )
        return entry

    def dst_entry(r: PkgRow) -> dict:
        entry = {
            "kind": r.kind, "arch": r.arch, "channel": r.channel,
            "name": r.name, "epoch": r.epoch, "version": r.version,
            "release": r.release, "nevr": r.nevr, "nevra": r.nevra,
        }
        if include_urls:
            entry["dest_url"] = r.url
        return entry

    payload = {
        "must_add": [src_entry(r) for r in findings.must_add],
        "must_remove": [dst_entry(r) for r in findings.must_remove],
        "unexpected_versions": [dst_entry(r) for r in findings.unexpected_versions],
        "misrouted": [
            {
                **dst_entry(m.row),
                "actual_channel": m.actual_channel,
                "expected_channel": m.expected_channel,
            }
            for m in findings.misrouted
        ],
        "summary": {
            "must_add": len(findings.must_add),
            "must_remove": len(findings.must_remove),
            "unexpected_versions": len(findings.unexpected_versions),
            "misrouted": len(findings.misrouted),
        },
    }
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


# ---------------------------------------------------------------------------
# Mode plumbing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Mode:
    name: str
    source_url: str
    source_layout: LayoutKind
    dest_url: str
    dest_layout: LayoutKind
    # Insecure TLS is required for any side that points at the koji IP
    # (raw IP, self-signed cert).
    source_insecure: bool
    dest_insecure: bool


MODES: dict[str, Mode] = {
    "koji-vs-blob": Mode(
        name="koji-vs-blob",
        source_url=KOJI_URL, source_layout=LayoutKind.KOJI, source_insecure=True,
        dest_url=BLOB_URL, dest_layout=LayoutKind.AZL, dest_insecure=False,
    ),
    "blob-vs-pmc": Mode(
        name="blob-vs-pmc",
        source_url=BLOB_URL, source_layout=LayoutKind.AZL, source_insecure=False,
        dest_url=PMC_URL, dest_layout=LayoutKind.AZL, dest_insecure=False,
    ),
    "koji-vs-pmc": Mode(
        name="koji-vs-pmc",
        source_url=KOJI_URL, source_layout=LayoutKind.KOJI, source_insecure=True,
        dest_url=PMC_URL, dest_layout=LayoutKind.AZL, dest_insecure=False,
    ),
}


def _repo_root_for_azldev() -> Path:
    """Walk up from this script to find the Azure Linux repo root.

    `azldev` must be invoked with cwd set to the repo containing
    `azldev.toml`. The script sits at `<repo>/scripts/repo/audit-repos.py`
    so two parents up is the right answer.
    """
    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent
    if not (candidate / "azldev.toml").exists():
        raise RuntimeError(
            f"could not locate azldev.toml above {here}; expected {candidate}"
        )
    return candidate


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-dir", required=True, metavar="DIR",
        help=(
            "Directory to write per-mode reports into. Required. Will be "
            "created if missing. Each mode produces a subdirectory with "
            "four files: nevr.txt, nevr.json, uri.txt, uri.json."
        ),
    )
    parser.add_argument(
        "--no-routing-audit", action="store_true",
        help=(
            "Skip the misrouted check across all modes. Removes the "
            "dependency on `azldev` and the surrounding Azure Linux TOML "
            "configuration. With this flag, MUST-ADD projected dest URLs "
            "are not emitted under uri.{txt,json}."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    outdir = Path(args.output_dir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    repo_root: Path | None = None
    if not args.no_routing_audit:
        repo_root = _repo_root_for_azldev()

    # Per-run memoisation. Keys are (prefix, layout) so identical sides
    # across modes (e.g. koji as source for both koji-vs-blob and
    # koji-vs-pmc) are only fetched and queried once. No on-disk caching
    # persists across script runs.
    sides: dict[tuple[str, LayoutKind], SideRows] = {}
    src_channel_maps: dict[tuple[str, LayoutKind], dict[tuple[str, str], str] | None] = {}
    dst_channel_maps: dict[tuple[str, LayoutKind], dict[tuple[str, str], str] | None] = {}

    def load_side(prefix: str, layout: LayoutKind, insecure: bool,
                  scratch: Path, label: str) -> SideRows:
        key = (prefix, layout)
        if key not in sides:
            ssl_ctx = _build_ssl_context(insecure)
            if insecure:
                warn(f"TLS verification disabled for {prefix}")
            sides[key] = _load_side(prefix, layout, ssl_ctx, scratch, label)
        return sides[key]

    summary_lines: list[str] = []
    any_findings = False

    with tempfile.TemporaryDirectory(prefix="audit-repos.") as tmp:
        scratch = Path(tmp)

        for mode in MODES.values():
            log("")
            log(f"=== mode: {mode.name} ===")
            src = load_side(
                _normalize_prefix(mode.source_url), mode.source_layout,
                mode.source_insecure, scratch, f"{mode.name}.source",
            )
            dst = load_side(
                _normalize_prefix(mode.dest_url), mode.dest_layout,
                mode.dest_insecure, scratch, f"{mode.name}.dest",
            )
            findings = _compare(src, dst)

            channel_map: dict[tuple[str, str], str] | None = None
            if not args.no_routing_audit:
                assert repo_root is not None
                src_key = (_normalize_prefix(mode.source_url), mode.source_layout)
                if src_key not in src_channel_maps:
                    src_channel_maps[src_key] = _query_azldev_channels(
                        src.rows, scratch, repo_root,
                        f"src.{mode.source_layout.value}",
                    )
                channel_map = src_channel_maps[src_key]

                dst_key = (_normalize_prefix(mode.dest_url), mode.dest_layout)
                if dst_key not in dst_channel_maps:
                    dst_channel_maps[dst_key] = _query_azldev_channels(
                        dst.rows, scratch, repo_root,
                        f"dst.{mode.dest_layout.value}",
                    )
                dest_map = dst_channel_maps[dst_key]
                if dest_map is None:
                    warn(f"routing audit skipped for {mode.name} (azldev unavailable)")
                else:
                    findings.misrouted = _compute_misrouted(
                        dst, findings.must_remove, dest_map,
                    )

            # Write four files for this mode.
            mode_dir = outdir / mode.name
            mode_dir.mkdir(parents=True, exist_ok=True)
            for show in (ShowKey.NEVR, ShowKey.URI):
                with open(mode_dir / f"{show.value}.txt", "w") as fh:
                    _render_text(
                        fh, findings, show=show,
                        dest_prefix=_normalize_prefix(mode.dest_url),
                        azldev_channel_by_name=channel_map,
                    )
                with open(mode_dir / f"{show.value}.json", "w") as fh:
                    _render_json(
                        fh, findings, show=show,
                        dest_prefix=_normalize_prefix(mode.dest_url),
                        azldev_channel_by_name=channel_map,
                    )

            counts = (
                len(findings.must_add), len(findings.must_remove),
                len(findings.unexpected_versions), len(findings.misrouted),
            )
            summary_lines.append(
                f"  {mode.name:<14s}  "
                f"add={counts[0]:<5d}  remove={counts[1]:<5d}  "
                f"unexpected={counts[2]:<5d}  misrouted={counts[3]:<5d}  "
                f"-> {mode_dir}"
            )
            if any(counts):
                any_findings = True

    # Final summary.
    log("")
    log("=== summary ===")
    for line in summary_lines:
        log(line)
    log(f"reports written under: {outdir}")

    return 1 if any_findings else 0


if __name__ == "__main__":
    sys.exit(main())
