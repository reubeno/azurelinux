#!/usr/bin/env python3
"""Print a one-line summary of the latest repoclosure findings.

Reads the JSON outputs of ``scripts/split-repo-by-channel.py`` and prints
the totals for the ``base`` and ``base+sdk`` scopes. Used as the standard
"did this change make things better?" check between iteration steps in the
channel-rebalance workflow.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPLIT_DIR = REPO_ROOT / "base/build/work/scratch/repo-split"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--split-dir", default=str(DEFAULT_SPLIT_DIR))
    args = p.parse_args()
    d = Path(args.split_dir)
    rc = 0
    for scope in ("base", "base+sdk"):
        f = d / f"repoclosure-{scope}.findings.json"
        if not f.exists():
            print(f"{scope:9s}  (missing: {f})", file=sys.stderr)
            rc = 1
            continue
        t = json.loads(f.read_text())["totals"]
        print(f"{scope:9s} raw={t['raw']:5d} suppressed={t['suppressed']:4d} "
              f"remaining={t['remaining']:5d}")
    # Also show SRPM consistency if present.
    sc = d / "srpm-consistency.json"
    if sc.exists():
        info = json.loads(sc.read_text())
        t = info.get("totals", {})
        cross = t.get("cross_channel_srpms", "?")
        cov = t.get("covered_by_allowlist", "?")
        unc = t.get("uncovered", "?")
        print(f"srpm      cross-channel={cross} covered={cov} uncovered={unc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
