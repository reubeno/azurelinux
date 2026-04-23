#!/usr/bin/env python3
"""Surgically move RPMs between the rpm-base and rpm-sdk publish channels.

Operates on the package-list TOML files under ``base/packages/`` plus the
SRPM consistency allow-list under ``scripts/``. Used to drive the iterative
"shrink the rpm-base repoclosure violations" workflow documented in
``.github/skills/skill-rebalance-channels/SKILL.md``.

Subcommands
-----------
* ``promote <srpm>``      — move every binary RPM produced by an SRPM from
                            sdk to base.
* ``demote <srpm>``       — same, but from base to sdk.
* ``carve <srpm> <pkg>...``
                          — keep an SRPM's bulk in base but move *specific*
                            sub-packages to sdk via the curated exceptions
                            list, and add a matching entry to the SRPM
                            consistency allow-list.
* ``remove <srpm>``       — drop every binary RPM produced by an SRPM from
                            the base publish list (does NOT edit comp.toml;
                            removing a component definition is a separate
                            manual step).
* ``analyze``             — read the latest repoclosure findings and emit a
                            ranked, actionable table of next-best moves.

The ``promote/demote/carve/remove`` commands need a populated
``base/build/work/scratch/repo-split/`` (run ``scripts/split-repo-by-channel.py``
first). They use ``createrepo_c`` to enumerate which binary RPMs each SRPM
produces and rewrite the TOMLs in place, preserving formatting where
possible.

The ``analyze`` command reads ``repoclosure-base.findings.json`` and clusters
findings by consumer SRPM, provider SRPM, and shared-SRPM-with-base sibling,
then emits an actionable table (markdown).

This script is intentionally TOML-format-aware via regex rather than a full
TOML round-trip parser: the package-list files are simple flat string-array
sections and using regex preserves comment placement, blank lines, and the
hand-curated grouping inside ``exceptions.packages.toml``.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

import createrepo_c as cr

# ---------------------------------------------------------------------------
# Paths (resolved relative to repo root, which we deduce from this file).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_TOML = REPO_ROOT / "base/packages/base.packages.toml"
SDK_TOML = REPO_ROOT / "base/packages/sdk.packages.toml"
EXC_TOML = REPO_ROOT / "base/packages/exceptions.packages.toml"
ALLOWLIST_TOML = REPO_ROOT / "scripts/srpm-consistency-allowlist.toml"
DEFAULT_SPLIT_DIR = REPO_ROOT / "base/build/work/scratch/repo-split"


# ---------------------------------------------------------------------------
# Repodata helpers
# ---------------------------------------------------------------------------

def _open_primary(repo_dir: Path) -> str:
    """Return the path to the primary.xml.* under ``repo_dir/repodata/``."""
    rm = cr.Repomd()
    cr.xml_parse_repomd(str(repo_dir / "repodata" / "repomd.xml"), rm,
                        lambda *_: True)
    for r in rm.records:
        if r.type == "primary":
            return str(repo_dir / r.location_href)
    raise RuntimeError(f"no primary.xml in {repo_dir}")


def _srpm_name(sourcerpm: str) -> str:
    """Strip ``-version-release.src.rpm`` to get the bare SRPM name."""
    s = sourcerpm
    if s.endswith(".src.rpm"):
        s = s[: -len(".src.rpm")]
    # Drop release then version (last two ``-`` separated chunks).
    s = s.rsplit("-", 1)[0]
    s = s.rsplit("-", 1)[0]
    return s


def pkgs_for_srpm(channel_dir: Path, srpm: str) -> list[str]:
    """Return the sorted list of binary RPM names produced by ``srpm`` in
    ``channel_dir``."""
    pri = _open_primary(channel_dir)
    out: list[str] = []

    def cb(pkg):
        if _srpm_name(pkg.rpm_sourcerpm or "") == srpm:
            out.append(pkg.name)

    cr.xml_parse_primary(pri, pkgcb=cb, do_files=False,
                         warningcb=lambda *_: True)
    return sorted(set(out))


def consumers_of(channel_dir: Path, capability_substr: str) -> list[tuple[str, str]]:
    """Return (consumer_pkg, requires_string) pairs for every package in
    ``channel_dir`` whose Requires contain ``capability_substr``."""
    pri = _open_primary(channel_dir)
    out: list[tuple[str, str]] = []

    def cb(pkg):
        for req in pkg.requires:
            cap = req[0]
            if capability_substr in cap:
                out.append((pkg.name, cap))

    cr.xml_parse_primary(pri, pkgcb=cb, do_files=False,
                         warningcb=lambda *_: True)
    return out


# ---------------------------------------------------------------------------
# TOML edit helpers (regex-based, formatting-preserving)
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r'^\s*"([^"]+)",?\s*\n')


def remove_pkgs(toml: Path, pkgs: set[str]) -> list[str]:
    """Delete any ``"name",`` line whose name is in ``pkgs``. Returns the
    list of names actually removed (preserves duplicates' first occurrence
    only — the toml shouldn't have dupes)."""
    if not pkgs:
        return []
    text = toml.read_text()
    removed: list[str] = []
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = _LINE_RE.match(line)
        if m and m.group(1) in pkgs:
            removed.append(m.group(1))
            continue
        out.append(line)
    toml.write_text("".join(out))
    return removed


def add_pkgs_sorted(toml: Path, pkgs: set[str]) -> int:
    """Insert ``pkgs`` into the (single) ``packages = [...]`` array, merging
    with existing entries and sorting alphabetically. Returns the count of
    new entries actually added. Strips any pre-existing comment lines inside
    the array.
    """
    if not pkgs:
        return 0
    text = toml.read_text()
    m = re.search(r"(packages\s*=\s*\[)(.*?)(\n\])", text, re.S)
    if not m:
        raise RuntimeError(f"no packages = [...] block found in {toml}")
    existing = re.findall(r'"([^"]+)"', m.group(2))
    merged = sorted(set(existing) | pkgs)
    body = "\n" + "\n".join(f'    "{n}",' for n in merged)
    new_text = text[: m.start(2)] + body + text[m.end(2):]
    toml.write_text(new_text)
    return len(merged) - len(existing)


def append_pkgs_block(toml: Path, comment: str, pkgs: list[str]) -> None:
    """Append a commented block of ``pkgs`` lines at the end of the
    ``packages = [...]`` array — used for ``exceptions.packages.toml`` where
    we want to keep human-curated grouping rather than re-sort.
    """
    text = toml.read_text()
    m = re.search(r"(packages\s*=\s*\[)(.*?)(\n\])", text, re.S)
    if not m:
        raise RuntimeError(f"no packages = [...] block found in {toml}")
    block = f"\n    # {comment}\n" + "".join(f'    "{p}",\n' for p in pkgs)
    # Insert immediately before the closing "\n]".
    new_text = text[: m.start(3)] + block.rstrip("\n") + text[m.start(3):]
    toml.write_text(new_text)


def append_allowlist_entry(toml: Path, srpm: str, expected_channel: str,
                           allowed_in_other: list[str], reason: str) -> None:
    """Append one ``[[exception]]`` block to the SRPM-consistency allow-list."""
    text = toml.read_text()
    if not text.endswith("\n"):
        text += "\n"
    pkgs_str = ", ".join(f'"{p}"' for p in allowed_in_other)
    entry = (
        "\n[[exception]]\n"
        f'srpm             = "{srpm}"\n'
        f'expected_channel = "{expected_channel}"\n'
        f'allowed_in_other = [{pkgs_str}]\n'
        f'reason           = "{reason}"\n'
    )
    toml.write_text(text + entry)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_promote(args: argparse.Namespace) -> int:
    sdk_dir = Path(args.split_dir) / "sdk"
    pkgs = pkgs_for_srpm(sdk_dir, args.srpm)
    if not pkgs:
        print(f"WARN: no binary RPMs in sdk for SRPM {args.srpm!r}",
              file=sys.stderr)
        return 1
    print(f"{args.srpm}: {len(pkgs)} sub-pkg(s) in sdk:")
    for p in pkgs:
        print(f"  {p}")
    if args.dry_run:
        return 0
    pset = set(pkgs)
    removed = remove_pkgs(SDK_TOML, pset)
    added = add_pkgs_sorted(BASE_TOML, pset)
    miss = pset - set(removed)
    print(f"  removed {len(removed)} from {SDK_TOML.name}, "
          f"added {added} to {BASE_TOML.name}")
    if miss:
        print(f"  WARNING: not found in {SDK_TOML.name}: {sorted(miss)}",
              file=sys.stderr)
    return 0


def cmd_demote(args: argparse.Namespace) -> int:
    base_dir = Path(args.split_dir) / "base"
    pkgs = pkgs_for_srpm(base_dir, args.srpm)
    if not pkgs:
        print(f"WARN: no binary RPMs in base for SRPM {args.srpm!r}",
              file=sys.stderr)
        return 1
    print(f"{args.srpm}: {len(pkgs)} sub-pkg(s) in base:")
    for p in pkgs:
        print(f"  {p}")
    if args.dry_run:
        return 0
    pset = set(pkgs)
    removed = remove_pkgs(BASE_TOML, pset)
    added = add_pkgs_sorted(SDK_TOML, pset)
    miss = pset - set(removed)
    print(f"  removed {len(removed)} from {BASE_TOML.name}, "
          f"added {added} to {SDK_TOML.name}")
    if miss:
        print(f"  WARNING: not found in {BASE_TOML.name}: {sorted(miss)}",
              file=sys.stderr)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    base_dir = Path(args.split_dir) / "base"
    pkgs = pkgs_for_srpm(base_dir, args.srpm)
    if not pkgs:
        print(f"WARN: no binary RPMs in base for SRPM {args.srpm!r}",
              file=sys.stderr)
        return 1
    print(f"{args.srpm}: {len(pkgs)} sub-pkg(s) in base will be removed:")
    for p in pkgs:
        print(f"  {p}")
    print()
    print("NOTE: this only edits base.packages.toml. To remove the component")
    print("      from the distro entirely, also delete the [components.<name>]")
    print("      line from base/comps/components.toml (and any per-comp dir).")
    if args.dry_run:
        return 0
    removed = remove_pkgs(BASE_TOML, set(pkgs))
    print(f"  removed {len(removed)} from {BASE_TOML.name}")
    return 0


def cmd_carve(args: argparse.Namespace) -> int:
    pkgs = sorted(set(args.pkg))
    if not args.exception_comment:
        print("ERROR: --exception-comment is required for carve",
              file=sys.stderr)
        return 2
    if not args.allowlist_reason:
        print("ERROR: --allowlist-reason is required for carve",
              file=sys.stderr)
        return 2
    print(f"carving {args.srpm}: {pkgs}")
    if args.dry_run:
        return 0
    removed = remove_pkgs(BASE_TOML, set(pkgs))
    miss = set(pkgs) - set(removed)
    print(f"  removed from {BASE_TOML.name}: {removed}")
    if miss:
        print(f"  WARNING: not found in {BASE_TOML.name}: {sorted(miss)}",
              file=sys.stderr)
    append_pkgs_block(EXC_TOML, args.exception_comment, pkgs)
    append_allowlist_entry(ALLOWLIST_TOML, args.srpm, "base", pkgs,
                           args.allowlist_reason)
    print(f"  added exceptions block + allowlist entry")
    return 0


# ---------------------------------------------------------------------------
# analyze: cluster repoclosure findings into actionable candidate table
# ---------------------------------------------------------------------------

def _load_findings(split_dir: Path) -> list[dict]:
    p = split_dir / "repoclosure-base.findings.json"
    return json.loads(p.read_text())["findings"]


def _build_srpm_index(split_dir: Path) -> dict[tuple[str, str], str]:
    """Map (channel, binary_pkg_name) -> srpm_name."""
    out: dict[tuple[str, str], str] = {}
    for ch in ("base", "sdk"):
        d = split_dir / ch
        if not (d / "repodata" / "repomd.xml").exists():
            continue
        pri = _open_primary(d)

        def cb(pkg, _ch=ch):
            out[(_ch, pkg.name)] = _srpm_name(pkg.rpm_sourcerpm or "")

        cr.xml_parse_primary(pri, pkgcb=cb, do_files=False,
                             warningcb=lambda *_: True)
    return out


def _build_provider_index(split_dir: Path) -> dict[tuple[str, str], list[str]]:
    """Map (channel, capability_string) -> [provider_pkg_name, ...].

    Only indexes capabilities that look like missing-dep candidates: package
    names, file-paths, soname strings, and ``foo(bar)`` style provides. We
    index the literal string of every provide entry plus the package name.
    """
    out: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for ch in ("base", "sdk"):
        d = split_dir / ch
        if not (d / "repodata" / "repomd.xml").exists():
            continue
        pri = _open_primary(d)

        def cb(pkg, _ch=ch):
            out[(_ch, pkg.name)].append(pkg.name)
            for prov in pkg.provides:
                out[(_ch, prov[0])].append(pkg.name)
            for fname in pkg.files:
                # files are (type, path_dir, basename); we record full paths
                full = (fname[1] or "") + (fname[2] or "")
                if full:
                    out[(_ch, full)].append(pkg.name)

        cr.xml_parse_primary(pri, pkgcb=cb, do_files=True,
                             warningcb=lambda *_: True)
    return out


def cmd_analyze(args: argparse.Namespace) -> int:
    split_dir = Path(args.split_dir)
    findings = _load_findings(split_dir)
    if not findings:
        print("No findings to analyze.")
        return 0

    print(f"Loading SRPM and provider indexes from {split_dir} ...",
          file=sys.stderr)
    srpm_of = _build_srpm_index(split_dir)
    providers = _build_provider_index(split_dir)

    # For each finding, identify:
    #   * consumer SRPM (the SRPM whose sub-pkg has an unresolved dep)
    #   * provider SRPM(s) in sdk that could satisfy it
    finding_rows = []
    for f in findings:
        cname = f["consumer_name"]
        req = f["requires"]
        consumer_srpm = srpm_of.get(("base", cname), "?")
        # Look up providers in sdk by exact capability match.
        prov_pkgs = providers.get(("sdk", req), [])
        prov_srpms = sorted({srpm_of.get(("sdk", p), "?") for p in prov_pkgs})
        finding_rows.append({
            "consumer": cname,
            "consumer_srpm": consumer_srpm,
            "req": req,
            "providers": prov_pkgs,
            "provider_srpms": prov_srpms,
        })

    # Cluster by consumer SRPM.
    by_consumer_srpm = collections.defaultdict(list)
    for r in finding_rows:
        by_consumer_srpm[r["consumer_srpm"]].append(r)

    # For each consumer SRPM, count the distinct sub-packages with findings,
    # and the dominant provider SRPMs.
    rows = []
    for csrpm, items in by_consumer_srpm.items():
        sub_pkgs = sorted({i["consumer"] for i in items})
        prov_count = collections.Counter()
        sample_reqs = []
        for i in items:
            for ps in i["provider_srpms"]:
                prov_count[ps] += 1
            if len(sample_reqs) < 2:
                sample_reqs.append(f"{i['consumer']} -> {i['req']}")
        rows.append({
            "srpm": csrpm,
            "findings": len(items),
            "sub_pkgs_with_findings": sub_pkgs,
            "n_sub_pkgs_with_findings": len(sub_pkgs),
            "top_providers": prov_count.most_common(5),
            "sample": sample_reqs,
        })
    rows.sort(key=lambda r: -r["findings"])

    # Output: JSON to a file + markdown table to stdout.
    out_json = split_dir / "rebalance-candidates.json"
    out_json.write_text(json.dumps(rows, indent=2))
    print(f"Wrote candidate JSON: {out_json}", file=sys.stderr)

    print()
    print("# Rebalance-channel candidates")
    print()
    print(f"Total findings analyzed: {len(findings)}")
    print(f"Distinct consumer SRPMs: {len(rows)}")
    print()
    print("| Rank | Consumer SRPM | Findings | Sub-pkgs leaking | "
          "Top provider SRPMs (sdk) | Concrete example |")
    print("|---:|---|---:|---:|---|---|")
    for i, r in enumerate(rows[: args.top], 1):
        provs = ", ".join(f"{ps}({n})" for ps, n in r["top_providers"]) \
            or "(none-in-sdk)"
        ex = r["sample"][0] if r["sample"] else ""
        # Trim long requires for table readability.
        if len(ex) > 70:
            ex = ex[:67] + "..."
        leakers = r["n_sub_pkgs_with_findings"]
        print(f"| {i} | `{r['srpm']}` | {r['findings']} | {leakers} | "
              f"{provs} | `{ex}` |")
    print()
    print("**Reading the table:**")
    print("- *Consumer SRPM*: the base-side SRPM whose binary RPM(s) have "
          "unresolved deps.")
    print("- *Sub-pkgs leaking*: how many distinct binary RPMs from this SRPM "
          "are flagged. **=1 → strong carve candidate.**")
    print("- *Top provider SRPMs*: where the missing capabilities live "
          "(in sdk). **One dominant provider → cluster is solvable by "
          "promoting that one SRPM (small) or carving the consumer.**")
    print()
    print("Next-step recipe per row:")
    print("  1. If sub-pkgs-leaking == 1 (or a small named subset, e.g. "
          "`*-qt5*`, `*-gui`, `*+extra`), carve those sub-pkgs:")
    print("       scripts/rebalance-channel.py carve <srpm> "
          "--exception-comment '...' --allowlist-reason '...' <sub-pkg> ...")
    print("  2. If a single small provider SRPM dominates (e.g. <10 sub-pkgs) "
          "and is broadly useful, promote it:")
    print("       scripts/rebalance-channel.py promote <provider-srpm>")
    print("  3. If the consumer SRPM itself is sdk-tier (build tooling, GUI "
          "stack, niche), demote the whole SRPM:")
    print("       scripts/rebalance-channel.py demote <srpm>")
    print("  4. Always re-run scripts/split-repo-by-channel.py and "
          "scripts/repo-split-summary.py between steps; commit each step "
          "separately.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Surgically move RPMs between rpm-base and rpm-sdk.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "All commands are idempotent in the sense that they re-read the "
            "TOMLs each time. Run scripts/split-repo-by-channel.py first to "
            "generate the repodata under base/build/work/scratch/repo-split/, "
            "then iterate: pick a candidate (analyze), apply a change "
            "(promote/demote/carve), re-run split, commit."
        ),
    )
    p.add_argument("--split-dir", default=str(DEFAULT_SPLIT_DIR),
                   help=f"path to the split-repo dir (default: "
                        f"{DEFAULT_SPLIT_DIR.relative_to(REPO_ROOT)})")
    p.add_argument("--dry-run", action="store_true",
                   help="print the changes that would be made; do not edit")

    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("promote", help="move an SRPM's pkgs from sdk to base")
    pr.add_argument("srpm")
    pr.set_defaults(func=cmd_promote)

    de = sub.add_parser("demote", help="move an SRPM's pkgs from base to sdk")
    de.add_argument("srpm")
    de.set_defaults(func=cmd_demote)

    rm = sub.add_parser("remove",
                        help="drop an SRPM's pkgs from base.packages.toml")
    rm.add_argument("srpm")
    rm.set_defaults(func=cmd_remove)

    cv = sub.add_parser("carve",
                        help="move specific sub-pkgs of an SRPM into the "
                             "exceptions list (publishes to sdk)")
    cv.add_argument("srpm")
    cv.add_argument("pkg", nargs="+",
                    help="sub-package names to carve out")
    cv.add_argument("--exception-comment", required=True,
                    help="one-line comment placed above the entries in "
                         "exceptions.packages.toml")
    cv.add_argument("--allowlist-reason", required=True,
                    help="reason= text for the SRPM consistency allow-list "
                         "entry")
    cv.set_defaults(func=cmd_carve)

    an = sub.add_parser("analyze",
                        help="cluster repoclosure findings into a ranked "
                             "candidate table")
    an.add_argument("--top", type=int, default=25,
                    help="number of top consumer-SRPM rows to print "
                         "(default: 25)")
    an.set_defaults(func=cmd_analyze)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
