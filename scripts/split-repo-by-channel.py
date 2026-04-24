#!/usr/bin/env python3
"""Split an RPM repository into per-channel sub-repositories.

Reads Azure Linux package publishing metadata from ``azldev package list``,
downloads upstream repodata via ``dnf``, and uses the createrepo_c Python API
to produce filtered per-channel repos (e.g. ``base/``, ``sdk/``) whose
repodata references the original remote RPMs.

Dependencies: python3-createrepo_c, dnf, azldev
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import createrepo_c as cr

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_URL = (
    "https://stcontroltowerdevjwisitg.blob.core.windows.net"
    "/daily-repo-dev/20260419/x86_64"
)
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "repoclosure-allowlist.toml"
DEFAULT_SRPM_ALLOWLIST = (
    Path(__file__).resolve().parent / "srpm-consistency-allowlist.toml"
)


# ---------------------------------------------------------------------------
# Phase 1: Gather publishing metadata from azldev
# ---------------------------------------------------------------------------

def load_publish_metadata(repo_root: Path) -> dict[str, str]:
    """Run ``azldev package list -a -O json`` and return a mapping of
    binary package name → channel directory name (e.g. ``"base"``).

    Packages with channel ``"none"`` are excluded.
    """
    result = subprocess.run(
        ["azldev", "package", "list", "-a", "-O", "json"],
        capture_output=True,
        text=True,
        cwd=repo_root,
        check=True,
    )
    entries = json.loads(result.stdout)

    pkg_to_channel: dict[str, str] = {}
    for entry in entries:
        name = entry["packageName"]
        channel = entry["publishChannel"]
        if channel == "none":
            continue
        # Strip the "rpm-" prefix to get the directory name.
        if channel.startswith("rpm-"):
            channel = channel[len("rpm-"):]
        pkg_to_channel[name] = channel

    return pkg_to_channel


# ---------------------------------------------------------------------------
# Phase 2: Download repodata via dnf
# ---------------------------------------------------------------------------

def download_repodata(repo_url: str, cache_dir: Path) -> Path:
    """Download repo metadata files into *cache_dir*.

    Fetches ``repomd.xml``, parses it to discover the primary, filelists,
    and other metadata files, then downloads those directly.

    Returns the path to the directory containing the cached ``repodata/``
    (the parent of the ``repodata/`` directory).
    """
    repodata_dir = cache_dir / "repodata"
    repodata_dir.mkdir(parents=True, exist_ok=True)

    base = repo_url.rstrip("/")

    # Download repomd.xml first.
    repomd_url = f"{base}/repodata/repomd.xml"
    repomd_path = repodata_dir / "repomd.xml"
    print(f"    Fetching {repomd_url}")
    urllib.request.urlretrieve(repomd_url, repomd_path)

    # Parse repomd.xml to discover metadata file URLs.
    repomd = cr.Repomd()
    cr.xml_parse_repomd(str(repomd_path), repomd, lambda *_: True)

    for record in repomd.records:
        if record.type in ("primary", "filelists", "other"):
            url = f"{base}/{record.location_href}"
            dest = cache_dir / record.location_href
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"    Fetching {url}")
            urllib.request.urlretrieve(url, dest)

    return cache_dir


# ---------------------------------------------------------------------------
# Phase 3 & 4: Parse upstream repodata and write filtered per-channel repos
# ---------------------------------------------------------------------------

def _find_metadata_paths(repo_dir: Path) -> tuple[str, str, str]:
    """Parse repomd.xml and return (primary, filelists, other) paths."""
    repomd = cr.Repomd()
    cr.xml_parse_repomd(
        str(repo_dir / "repodata" / "repomd.xml"), repomd, lambda *_: True
    )

    primary = filelists = other = None
    for record in repomd.records:
        full = str(repo_dir / record.location_href)
        if record.type == "primary":
            primary = full
        elif record.type == "filelists":
            filelists = full
        elif record.type == "other":
            other = full

    if not (primary and filelists and other):
        sys.exit("error: repomd.xml missing primary/filelists/other records")

    return primary, filelists, other


def _count_packages_per_channel(
    primary_path: str, pkg_to_channel: dict[str, str]
) -> tuple[dict[str, int], int]:
    """Quick first pass over primary.xml to count packages per channel.

    Returns (channel_counts, unmatched_count).
    """
    counts: dict[str, int] = defaultdict(int)
    unmatched = 0

    def pkgcb(pkg):
        nonlocal unmatched
        channel = pkg_to_channel.get(pkg.name)
        if channel:
            counts[channel] += 1
        else:
            unmatched += 1

    cr.xml_parse_primary(primary_path, pkgcb=pkgcb, do_files=False,
                         warningcb=lambda *_: True)
    return dict(counts), unmatched


class _ChannelWriter:
    """Manages the repodata XML + SQLite writers for one channel."""

    def __init__(self, channel: str, output_dir: Path, pkg_count: int):
        self.channel = channel
        self.repodata_dir = output_dir / channel / "repodata"
        if self.repodata_dir.exists():
            shutil.rmtree(self.repodata_dir)
        self.repodata_dir.mkdir(parents=True, exist_ok=True)

        self.pri_xml_path = str(self.repodata_dir / "primary.xml.gz")
        self.fil_xml_path = str(self.repodata_dir / "filelists.xml.gz")
        self.oth_xml_path = str(self.repodata_dir / "other.xml.gz")

        self.pri_db_path = str(self.repodata_dir / "primary.sqlite")
        self.fil_db_path = str(self.repodata_dir / "filelists.sqlite")
        self.oth_db_path = str(self.repodata_dir / "other.sqlite")

        self.pri_xml = cr.PrimaryXmlFile(self.pri_xml_path)
        self.fil_xml = cr.FilelistsXmlFile(self.fil_xml_path)
        self.oth_xml = cr.OtherXmlFile(self.oth_xml_path)

        self.pri_db = cr.PrimarySqlite(self.pri_db_path)
        self.fil_db = cr.FilelistsSqlite(self.fil_db_path)
        self.oth_db = cr.OtherSqlite(self.oth_db_path)

        self.pri_xml.set_num_of_pkgs(pkg_count)
        self.fil_xml.set_num_of_pkgs(pkg_count)
        self.oth_xml.set_num_of_pkgs(pkg_count)

    def add_pkg(self, pkg):
        self.pri_xml.add_pkg(pkg)
        self.fil_xml.add_pkg(pkg)
        self.oth_xml.add_pkg(pkg)
        self.pri_db.add_pkg(pkg)
        self.fil_db.add_pkg(pkg)
        self.oth_db.add_pkg(pkg)

    def finish(self):
        self.pri_xml.close()
        self.fil_xml.close()
        self.oth_xml.close()

        repomd = cr.Repomd()

        records = [
            ("primary",      self.pri_xml_path, self.pri_db),
            ("filelists",    self.fil_xml_path, self.fil_db),
            ("other",        self.oth_xml_path, self.oth_db),
            ("primary_db",   self.pri_db_path,  None),
            ("filelists_db", self.fil_db_path,  None),
            ("other_db",     self.oth_db_path,  None),
        ]

        for name, path, db in records:
            rec = cr.RepomdRecord(name, path)
            rec.fill(cr.SHA256)
            if db is not None:
                db.dbinfo_update(rec.checksum)
                db.close()
            repomd.set_record(rec)

        repomd_path = self.repodata_dir / "repomd.xml"
        repomd_path.write_text(repomd.xml_dump())


def split_repo(
    pkg_to_channel: dict[str, str],
    upstream_dir: Path,
    output_dir: Path,
) -> tuple[dict[str, int], dict[str, str]]:
    """Parse upstream repodata and write per-channel filtered repos.

    Returns (channel → pkg count, binary-pkg-name → SRPM-name).
    """
    primary, filelists, other = _find_metadata_paths(upstream_dir)

    # First pass: count packages per channel for XML headers, capture pkg→srpm.
    channel_counts, unmatched = _count_packages_per_channel(
        primary, pkg_to_channel
    )

    if unmatched:
        print(
            f"warning: {unmatched} package(s) in upstream repo have no "
            f"publish metadata and will be excluded",
            file=sys.stderr,
        )

    channels = sorted(channel_counts)
    print(f"Channels to generate: {', '.join(channels)}")
    for ch in channels:
        print(f"  {ch}: {channel_counts[ch]} packages")

    # Open writers for each channel.
    writers: dict[str, _ChannelWriter] = {}
    for ch in channels:
        writers[ch] = _ChannelWriter(ch, output_dir, channel_counts[ch])

    # Second pass: stream all 3 metadata files together, dispatch to writers,
    # and capture pkg→SRPM mapping along the way.
    pkg_iter = cr.PackageIterator(
        primary_path=primary,
        filelists_path=filelists,
        other_path=other,
        warningcb=lambda *_: True,
    )

    pkg_to_srpm: dict[str, str] = {}
    written: dict[str, int] = defaultdict(int)
    for pkg in pkg_iter:
        srpm = pkg.rpm_sourcerpm or ""
        if srpm.endswith(".src.rpm"):
            srpm = srpm[: -len(".src.rpm")]
        # Strip -version-release.
        srpm_name = srpm.rsplit("-", 1)[0].rsplit("-", 1)[0] if srpm else ""
        if srpm_name:
            pkg_to_srpm[pkg.name] = srpm_name

        channel = pkg_to_channel.get(pkg.name)
        if channel and channel in writers:
            writers[channel].add_pkg(pkg)
            written[channel] += 1

    # Finalize: close XML files, compute checksums, write repomd.xml.
    for ch in channels:
        writers[ch].finish()
        print(f"  Wrote {written[ch]} packages to {output_dir / ch}/")

    return dict(written), pkg_to_srpm


# ---------------------------------------------------------------------------
# Phase 4.5: SRPM channel-consistency check
# ---------------------------------------------------------------------------

def load_srpm_allowlist(path: Path) -> dict[str, dict]:
    """Load SRPM-consistency allowlist; return {srpm: entry}."""
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    out: dict[str, dict] = {}
    for entry in data.get("exception", []) or []:
        srpm = entry.get("srpm")
        if not srpm:
            continue
        entry.setdefault("expected_channel", "")
        entry.setdefault("allowed_in_other", [])
        entry.setdefault("reason", "")
        out[srpm] = entry
    return out


def check_srpm_consistency(
    pkg_to_channel: dict[str, str],
    pkg_to_srpm: dict[str, str],
    output_dir: Path,
    allowlist: dict[str, dict] | None = None,
) -> int:
    """Verify that every SRPM publishes its binary RPMs to a single channel.

    Writes srpm-consistency.{txt,json}. Returns the number of *uncovered*
    violations (i.e., SRPMs with cross-channel sub-packages not allowlisted).
    """
    allowlist = allowlist or {}

    # Build SRPM -> {channel -> [pkg names]} for *published* packages only.
    srpm_to_chan_pkgs: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for pkg, channel in pkg_to_channel.items():
        srpm = pkg_to_srpm.get(pkg)
        if not srpm:
            continue
        srpm_to_chan_pkgs[srpm][channel].append(pkg)

    raw_violations: list[dict] = []
    for srpm, by_chan in srpm_to_chan_pkgs.items():
        if len(by_chan) > 1:
            raw_violations.append({
                "srpm": srpm,
                "channels": {ch: sorted(pkgs) for ch, pkgs in by_chan.items()},
                "total_pkgs": sum(len(p) for p in by_chan.values()),
            })

    # Apply allowlist: classify each violation as covered / partially-covered /
    # uncovered, and identify any unexpected "stray" pkgs that are not in
    # expected_channel and don't match any allowed_in_other glob.
    covered: list[dict] = []
    uncovered: list[dict] = []
    for v in raw_violations:
        entry = allowlist.get(v["srpm"])
        if not entry:
            uncovered.append({**v, "stray_pkgs": []})
            continue
        expected = entry["expected_channel"]
        patterns = entry["allowed_in_other"]
        stray: list[tuple[str, str]] = []  # (pkg, channel)
        for ch, pkgs in v["channels"].items():
            if ch == expected:
                continue
            for p in pkgs:
                if not any(fnmatch.fnmatchcase(p, pat) for pat in patterns):
                    stray.append((p, ch))
        annotated = {
            **v,
            "allowlist_entry": entry,
            "stray_pkgs": stray,
        }
        if stray:
            uncovered.append(annotated)
        else:
            covered.append(annotated)

    raw_violations.sort(key=lambda v: -v["total_pkgs"])
    covered.sort(key=lambda v: v["srpm"])
    uncovered.sort(key=lambda v: -v["total_pkgs"])

    txt_path = output_dir / "srpm-consistency.txt"
    json_path = output_dir / "srpm-consistency.json"

    with txt_path.open("w") as fh:
        fh.write(
            "# Policy: every binary RPM produced by an SRPM must publish to "
            "the same channel.\n"
            f"# Total cross-channel SRPMs : {len(raw_violations)}\n"
            f"# Covered by allowlist     : {len(covered)}\n"
            f"# Uncovered (real)         : {len(uncovered)}\n"
        )
        if uncovered:
            fh.write("\n## UNCOVERED VIOLATIONS\n")
            for v in uncovered:
                fh.write(
                    f"\nSRPM {v['srpm']}  ({v['total_pkgs']} pub pkgs)\n"
                )
                for ch in sorted(v["channels"]):
                    fh.write(f"  {ch}:\n")
                    for p in v["channels"][ch]:
                        fh.write(f"    {p}\n")
                if v.get("stray_pkgs"):
                    fh.write("  stray (not allowlisted):\n")
                    for p, ch in v["stray_pkgs"]:
                        fh.write(f"    {p}  (in {ch})\n")
        if covered:
            fh.write("\n## COVERED BY ALLOWLIST\n")
            for v in covered:
                e = v["allowlist_entry"]
                fh.write(
                    f"\nSRPM {v['srpm']}  expected={e['expected_channel']}  "
                    f"reason: {e['reason']}\n"
                )
                for ch in sorted(v["channels"]):
                    marker = "  " if ch == e["expected_channel"] else "* "
                    fh.write(f"  {marker}{ch}:\n")
                    for p in v["channels"][ch]:
                        fh.write(f"      {p}\n")

    json_path.write_text(json.dumps({
        "policy": (
            "every binary RPM produced by an SRPM must publish to the same "
            "channel"
        ),
        "totals": {
            "cross_channel_srpms": len(raw_violations),
            "covered_by_allowlist": len(covered),
            "uncovered": len(uncovered),
        },
        "uncovered": uncovered,
        "covered": covered,
    }, indent=2))

    print(
        f"    SRPM consistency: {len(raw_violations)} cross-channel SRPM(s) "
        f"({len(covered)} covered, {len(uncovered)} uncovered)"
    )
    if uncovered:
        # Per-channel-pair tally of uncovered.
        pair_counts: dict[tuple[str, ...], int] = defaultdict(int)
        for v in uncovered:
            pair_counts[tuple(sorted(v["channels"]))] += 1
        for pair, n in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
            print(f"      {' / '.join(pair):30} {n} uncovered SRPM(s)")
    print(f"    -> {txt_path.name}, {json_path.name}")

    return len(uncovered)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 5: Run dnf repoclosure against the generated repodata
# ---------------------------------------------------------------------------

# Parses dnf5 repoclosure output of the form:
#   package: <NEVRA> from <repo_id>
#     unresolved deps (<N>):
#       <reldep>
#       <reldep>
_PKG_LINE_RE = re.compile(r"^package:\s+(?P<nevra>\S+)\s+from\s+(?P<repo>\S+)")
_HDR_LINE_RE = re.compile(r"^\s+unresolved deps\b")
_DEP_LINE_RE = re.compile(r"^\s{4,}(?P<dep>\S.*?)\s*$")


def _nevra_to_name(nevra: str) -> str:
    """Strip epoch/version/release/arch from an NEVRA, returning just the name."""
    base = nevra.rsplit(".", 1)[0]            # drop .arch
    base = base.rsplit("-", 1)[0]             # drop -release
    base = base.rsplit("-", 1)[0]             # drop -version
    return base


def _classify_requires(req: str) -> tuple[str, str]:
    """Categorise a requires string. Returns (kind, basename).

    kind is one of: soname, python-dist, perl-mod, pkgconfig, cmake, rubygem,
    path, other-virtual, pkgname.
    basename is the most useful "thing being asked for" — for virtuals the
    inner argument, for sonames the .so name, for plain pkgs the package
    name without version constraints.
    """
    if re.match(r"^lib.*\.so", req):
        return "soname", re.split(r"[(=<>]", req, maxsplit=1)[0].strip()
    if req.startswith(("python3dist(", "python(")):
        m = re.match(r"^[a-zA-Z0-9_]+\(([^)]+)\)", req)
        return "python-dist", m.group(1) if m else req
    if req.startswith("perl("):
        m = re.match(r"^perl\(([^)]+)\)", req)
        return "perl-mod", m.group(1) if m else req
    if req.startswith("pkgconfig("):
        m = re.match(r"^pkgconfig\(([^)]+)\)", req)
        return "pkgconfig", m.group(1) if m else req
    if req.startswith("cmake("):
        m = re.match(r"^cmake\(([^)]+)\)", req)
        return "cmake", m.group(1) if m else req
    if req.startswith("rubygem("):
        m = re.match(r"^rubygem\(([^)]+)\)", req)
        return "rubygem", m.group(1) if m else req
    if req.startswith("/"):
        return "path", req
    head = req.split("(", 1)[0]
    if "(" in req and head.replace("-", "").isalpha():
        return "other-virtual", head
    return "pkgname", re.split(r"\s|[<>=]", req, maxsplit=1)[0].strip()


def parse_repoclosure_output(text: str) -> list[dict]:
    """Parse repoclosure text into a list of structured findings.

    Each finding is {consumer_nevra, consumer_name, consumer_repo,
    requires, req_kind, req_basename}.
    """
    results: list[dict] = []
    current_nevra: str | None = None
    current_repo: str | None = None
    in_deps = False
    for line in text.splitlines():
        m = _PKG_LINE_RE.match(line)
        if m:
            current_nevra = m.group("nevra")
            current_repo = m.group("repo")
            in_deps = False
            continue
        if current_nevra and _HDR_LINE_RE.match(line):
            in_deps = True
            continue
        if in_deps and current_nevra:
            m = _DEP_LINE_RE.match(line)
            if m:
                req = m.group("dep")
                kind, basename = _classify_requires(req)
                results.append({
                    "consumer_nevra": current_nevra,
                    "consumer_name": _nevra_to_name(current_nevra),
                    "consumer_repo": current_repo,
                    "requires": req,
                    "req_kind": kind,
                    "req_basename": basename,
                })
            elif line.strip() == "":
                continue
            else:
                in_deps = False
    return results


def load_allowlist(path: Path) -> list[dict]:
    """Load the TOML allowlist; return list of ignore entries."""
    if not path.exists():
        return []
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    entries = data.get("ignore", []) or []
    for entry in entries:
        entry.setdefault("scope", "any")
        entry.setdefault("reason", "")
        entry.setdefault("confidence", "")
        # Optional provenance fields for "overlay-pending" suppressions:
        #   overlay              path to the comp.toml that holds the overlay
        #                        which will eventually drop the dep from the
        #                        rebuilt RPMs (so that this allowlist entry
        #                        stops hitting and can be removed).
        #   verified_at_commit   git short-sha at which the overlay was last
        #                        verified locally via `azldev component build`
        #                        + `rpm -qpR` to actually have produced an
        #                        RPM that no longer carries the dep.
        entry.setdefault("overlay", "")
        entry.setdefault("verified_at_commit", "")
    return entries


def _entry_matches(entry: dict, scope: str, pkg_name: str, requires: str) -> bool:
    if entry["scope"] not in ("any", scope):
        return False
    if not fnmatch.fnmatchcase(pkg_name, entry["package"]):
        return False
    if not fnmatch.fnmatchcase(requires, entry["requires"]):
        return False
    return True


def run_repoclosure(
    scope_name: str,
    output_dir: Path,
    repos: list[tuple[str, Path]],
    check_repos: list[str],
    allowlist: list[dict],
) -> None:
    """Run `dnf repoclosure` over *repos*, checking *check_repos*.

    Writes three files into output_dir:
      repoclosure-<scope>.raw.txt
      repoclosure-<scope>.filtered.txt
      repoclosure-<scope>.allowlist-hits.txt
    """
    cmd: list[str] = [
        "dnf",
        "--disablerepo=*",
        "--no-plugins",
        "-q",
    ]
    cache_dir = output_dir / f".dnf-cache-{scope_name}"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    for repo_id, repo_path in repos:
        cmd.append(f"--repofrompath={repo_id},{repo_path}")
        # repofrompath creates the repo disabled-by-default in some setups;
        # explicitly enable it via setopt.
        cmd.append(f"--setopt={repo_id}.enabled=1")
        cmd.append(f"--setopt={repo_id}.gpgcheck=0")
    # Pin a fresh cache dir so dnf does not reuse stale metadata from a
    # previous invocation of this script (the on-disk paths are stable but
    # contents change).
    cmd.append(f"--setopt=cachedir={cache_dir}")
    cmd += ["repoclosure", f"--check={','.join(check_repos)}"]

    print(f"    $ {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # dnf repoclosure exits non-zero when unresolved deps exist; that's expected.
    raw_text = (proc.stdout or "") + (proc.stderr or "")

    raw_path = output_dir / f"repoclosure-{scope_name}.raw.txt"
    filtered_path = output_dir / f"repoclosure-{scope_name}.filtered.txt"
    hits_path = output_dir / f"repoclosure-{scope_name}.allowlist-hits.txt"

    raw_path.write_text(raw_text)

    findings = parse_repoclosure_output(raw_text)
    suppressed: list[tuple[dict, dict]] = []
    kept: list[dict] = []
    for f in findings:
        match = next(
            (e for e in allowlist
             if _entry_matches(e, scope_name, f["consumer_name"], f["requires"])),
            None,
        )
        if match:
            suppressed.append((f, match))
        else:
            kept.append(f)

    # Filtered report (text): regroup remaining unresolved deps by consumer.
    grouped: dict[str, list[str]] = defaultdict(list)
    for f in kept:
        grouped[f["consumer_nevra"]].append(f["requires"])
    with filtered_path.open("w") as fh:
        if not grouped:
            fh.write("# No unresolved dependencies after applying allowlist.\n")
        else:
            fh.write(
                f"# {sum(len(v) for v in grouped.values())} unresolved "
                f"dependency line(s) across {len(grouped)} package(s) "
                f"after applying allowlist.\n"
            )
            for nevra in sorted(grouped):
                fh.write(f"\nPackage {nevra}\n")
                for req in sorted(set(grouped[nevra])):
                    fh.write(f"  unresolved dependency: {req}\n")

    # Hits report (text).
    with hits_path.open("w") as fh:
        if not suppressed:
            fh.write("# No allowlist entries matched.\n")
        else:
            fh.write(
                f"# {len(suppressed)} unresolved dependency line(s) "
                f"suppressed by allowlist.\n"
            )
            for f, entry in suppressed:
                fh.write(
                    f"\nPackage {f['consumer_nevra']}  "
                    f"(matched package={entry['package']})\n"
                    f"  unresolved dependency: {f['requires']}\n"
                    f"  reason     : {entry.get('reason', '')}\n"
                    f"  confidence : {entry.get('confidence', '')}\n"
                )
                if entry.get("overlay"):
                    fh.write(f"  overlay    : {entry['overlay']}\n")
                if entry.get("verified_at_commit"):
                    fh.write(
                        f"  verified-at: {entry['verified_at_commit']}\n"
                    )

    # Structured JSON output for downstream analysis tooling.
    json_path = output_dir / f"repoclosure-{scope_name}.findings.json"
    json_payload = {
        "scope": scope_name,
        "command": cmd,
        "totals": {
            "raw": len(findings),
            "suppressed": len(suppressed),
            "remaining": len(kept),
        },
        "findings": findings,
        "suppressed": [
            {**f, "allowlist_entry": entry} for f, entry in suppressed
        ],
        "remaining": kept,
    }
    json_path.write_text(json.dumps(json_payload, indent=2))

    print(
        f"    [{scope_name}] {len(findings)} raw / "
        f"{len(suppressed)} suppressed / {len(kept)} remaining"
    )
    print(
        f"    -> {raw_path.name}, {filtered_path.name}, "
        f"{hits_path.name}, {json_path.name}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Split an RPM repository into per-channel sub-repositories."
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Directory to write channel sub-repos (e.g. base/, sdk/).",
    )
    parser.add_argument(
        "--repo-url",
        default=DEFAULT_REPO_URL,
        help="URL of the upstream RPM repository (default: %(default)s).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help="Path to the azurelinux repo root (default: auto-detected).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help="TOML file with intentional repoclosure-violation suppressions "
             "(default: %(default)s).",
    )
    parser.add_argument(
        "--srpm-allowlist",
        type=Path,
        default=DEFAULT_SRPM_ALLOWLIST,
        help="TOML file with per-SRPM channel-consistency exceptions "
             "(default: %(default)s).",
    )
    parser.add_argument(
        "--skip-repoclosure",
        action="store_true",
        help="Skip the post-split dnf repoclosure runs.",
    )
    parser.add_argument(
        "--no-fail-on-srpm-violations",
        action="store_true",
        help="Do not exit non-zero when uncovered SRPM-consistency "
             "violations are found.",
    )
    args = parser.parse_args()

    output_dir: Path = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1
    print("==> Loading publish metadata from azldev ...")
    pkg_to_channel = load_publish_metadata(args.repo_root)
    print(f"    {len(pkg_to_channel)} packages with publishing info")

    # Phase 2
    print("==> Downloading upstream repodata via dnf ...")
    cache_dir = output_dir / ".dnf-cache"
    upstream_dir = download_repodata(args.repo_url, cache_dir)
    print(f"    Cached to {upstream_dir}")

    # Phase 3 & 4
    print("==> Splitting repodata by channel ...")
    written, pkg_to_srpm = split_repo(pkg_to_channel, upstream_dir, output_dir)

    # Phase 4.5: SRPM channel-consistency
    print("==> Checking SRPM channel-consistency ...")
    srpm_allow = load_srpm_allowlist(args.srpm_allowlist)
    if srpm_allow:
        print(f"    Loaded {len(srpm_allow)} SRPM exception(s) from "
              f"{args.srpm_allowlist}")
    uncovered_srpm = check_srpm_consistency(
        pkg_to_channel, pkg_to_srpm, output_dir, allowlist=srpm_allow
    )

    # Cleanup
    shutil.rmtree(cache_dir, ignore_errors=True)

    # Phase 5: repoclosure
    if not args.skip_repoclosure:
        print("==> Running dnf repoclosure ...")
        allowlist = load_allowlist(args.allowlist)
        if allowlist:
            print(f"    Loaded {len(allowlist)} allowlist entr(ies) from "
                  f"{args.allowlist}")
        else:
            print(f"    No allowlist entries (looked at {args.allowlist})")

        base_dir = output_dir / "base"
        sdk_dir = output_dir / "sdk"

        if base_dir.exists():
            run_repoclosure(
                scope_name="base",
                output_dir=output_dir,
                repos=[("base", base_dir)],
                check_repos=["base"],
                allowlist=allowlist,
            )
            if sdk_dir.exists():
                run_repoclosure(
                    scope_name="base+sdk",
                    output_dir=output_dir,
                    repos=[("base", base_dir), ("sdk", sdk_dir)],
                    check_repos=["base", "sdk"],
                    allowlist=allowlist,
                )
            else:
                print("    (no sdk/ repodata generated; skipping base+sdk run)")
        else:
            print("    (no base/ repodata generated; skipping repoclosure)")

    # Summary
    total = sum(written.values())
    print(f"\nDone. {total} packages across {len(written)} channel(s).")
    for ch in sorted(written):
        print(f"  {ch}: {output_dir / ch}/  ({written[ch]} packages)")

    if uncovered_srpm and not args.no_fail_on_srpm_violations:
        print(
            f"\nERROR: {uncovered_srpm} SRPM-consistency violation(s) not "
            f"covered by allowlist (see srpm-consistency.txt).",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
