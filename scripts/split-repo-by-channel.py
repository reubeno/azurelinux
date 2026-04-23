#!/usr/bin/env python3
"""Split an RPM repository into per-channel sub-repositories.

Reads Azure Linux package publishing metadata from ``azldev package list``,
downloads upstream repodata via ``dnf``, and uses the createrepo_c Python API
to produce filtered per-channel repos (e.g. ``base/``, ``sdk/``) whose
repodata references the original remote RPMs.

Dependencies: python3-createrepo_c, dnf, azldev
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import createrepo_c as cr

DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO_URL = (
    "https://stcontroltowerdevjwisitg.blob.core.windows.net"
    "/daily-repo-dev/20260419/x86_64"
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
) -> dict[str, int]:
    """Parse upstream repodata and write per-channel filtered repos.

    Returns a dict of channel → package count written.
    """
    primary, filelists, other = _find_metadata_paths(upstream_dir)

    # First pass: count packages per channel for XML headers.
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

    # Second pass: stream all 3 metadata files together, dispatch to writers.
    pkg_iter = cr.PackageIterator(
        primary_path=primary,
        filelists_path=filelists,
        other_path=other,
        warningcb=lambda *_: True,
    )

    written: dict[str, int] = defaultdict(int)
    for pkg in pkg_iter:
        channel = pkg_to_channel.get(pkg.name)
        if channel and channel in writers:
            writers[channel].add_pkg(pkg)
            written[channel] += 1

    # Finalize: close XML files, compute checksums, write repomd.xml.
    for ch in channels:
        writers[ch].finish()
        print(f"  Wrote {written[ch]} packages to {output_dir / ch}/")

    return dict(written)


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
    written = split_repo(pkg_to_channel, upstream_dir, output_dir)

    # Cleanup
    shutil.rmtree(cache_dir, ignore_errors=True)

    # Summary
    total = sum(written.values())
    print(f"\nDone. {total} packages across {len(written)} channel(s).")
    for ch in sorted(written):
        print(f"  {ch}: {output_dir / ch}/  ({written[ch]} packages)")


if __name__ == "__main__":
    main()
