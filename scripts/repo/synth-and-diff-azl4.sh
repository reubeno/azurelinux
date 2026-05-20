#!/usr/bin/env bash
# synth-and-diff-azl4.sh — synthesize AZL4/latest repos from Koji and diff against PMC beta.
#
# Usage:
#   ./scripts/repo/synth-and-diff-azl4.sh <output-dir>
#
# This script:
#   1. Runs synthesize-repodata.py against Koji dist-repo inputs (main + debuginfo + srpms)
#      placing the synthesized AZL-layout repos under <output-dir>/synth/
#   2. Runs diff-azl-repos.py comparing PMC beta (old) vs the synth output (new)
#      writing text + JSON results to <output-dir>/
#
# Requirements:
#   - azldev on PATH
#   - python3 with createrepo_c bindings
#   - Network access to Koji (20.225.0.246) and packages.microsoft.com

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# --- Koji dist-repo URLs (azl4/latest) ---
KOJI_BASE="https://20.225.0.246/kojifiles/repos-dist/azl4/latest"
KOJI_MAIN="${KOJI_BASE}/\$basearch"
KOJI_DEBUG="${KOJI_BASE}/\$basearch/debug"
KOJI_SRPMS="${KOJI_BASE}/src"

# --- PMC beta (Standard AZL Repo Layout) ---
PMC_PREFIX="https://packages.microsoft.com/azurelinux/4.0/beta/"

# --- Parse args ---
if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <output-dir>" >&2
    exit 1
fi

OUTPUT_DIR="$(realpath -m "$1")"
SYNTH_DIR="${OUTPUT_DIR}/synth"
DIFF_TEXT="${OUTPUT_DIR}/diff.txt"
DIFF_JSON="${OUTPUT_DIR}/diff.json"
SYNTH_LOG="${OUTPUT_DIR}/synth.log"

mkdir -p "$OUTPUT_DIR"

echo "==> Output directory: ${OUTPUT_DIR}"
echo "==> Synth output:     ${SYNTH_DIR}/"
echo "==> Diff results:     ${DIFF_TEXT}, ${DIFF_JSON}"
echo ""

# --- Step 1: Synthesize ---
echo "==> Step 1: Running synthesize-repodata.py ..."
echo "    Koji main:      ${KOJI_MAIN}"
echo "    Koji debuginfo: ${KOJI_DEBUG}"
echo "    Koji srpms:     ${KOJI_SRPMS}"
echo ""

python3 "${SCRIPT_DIR}/synthesize-repodata.py" \
    --output-dir "$SYNTH_DIR" \
    --repo "main:${KOJI_MAIN}" \
    --repo "debuginfo:${KOJI_DEBUG}" \
    --repo "srpms:${KOJI_SRPMS}" \
    --repo-root "$REPO_ROOT" \
    --insecure \
    2>&1 | tee "$SYNTH_LOG"

echo ""
echo "==> Step 1 complete. Synth output in: ${SYNTH_DIR}/"
echo ""

# --- Step 2: Diff ---
echo "==> Step 2: Running diff-azl-repos.py (PMC beta vs synth output) ..."
echo "    Old (PMC):  ${PMC_PREFIX}"
echo "    New (synth): file://${SYNTH_DIR}/"
echo ""

python3 "${SCRIPT_DIR}/diff-azl-repos.py" \
    --old "$PMC_PREFIX" \
    --new "file://${SYNTH_DIR}/" \
    --compare-by name \
    --show location \
    --insecure \
    > "$DIFF_TEXT" 2>&1 || true

python3 "${SCRIPT_DIR}/diff-azl-repos.py" \
    --old "$PMC_PREFIX" \
    --new "file://${SYNTH_DIR}/" \
    --compare-by name \
    --show name \
    --insecure \
    -O json \
    > "$DIFF_JSON" 2>&1 || true

echo ""
echo "==> Step 2 complete."
echo "    Text diff: ${DIFF_TEXT}"
echo "    JSON diff: ${DIFF_JSON}"
echo ""

# --- Summary ---
echo "==> Quick summary of diff:"
grep -E "^== " "$DIFF_TEXT" || true
echo ""
echo "Done."
