---
name: skill-rebalance-channels
description: "[Skill] Iteratively shrink rpm-base repoclosure violations by surgically promoting / demoting / carving SRPMs between the rpm-base and rpm-sdk publish channels. Use when working on the channel-split TOMLs (base/packages/*.packages.toml) or when the user asks for actionable next-step candidates to reduce dnf repoclosure unresolveds. Triggers: rebalance channels, repoclosure violations, base/sdk split, promote/demote/carve SRPM, channel split surgery, repo-split, channel rebalance."
---

# Rebalance rpm-base / rpm-sdk channel split

This skill drives the iterative inner-loop where we **shrink the count of
`dnf repoclosure` violations against `rpm-base`** by surgically moving
sub-packages and whole SRPMs between the `rpm-base` and `rpm-sdk` publish
channels — without bloating `rpm-base` with material that is not
production-supported, and without losing internal SRPM-channel consistency.

There are two distinct workflows here:

1. **Analyze** — produce a ranked, *actionable* table of next-step
   candidates from the current repoclosure findings.
2. **Apply** — execute one or more user-approved candidate actions, each
   followed by a full re-validate-and-commit loop.

Both are scripted; the skill exists to keep human / agent judgment in the
loop, because most decisions are policy decisions (e.g. *should the GUI
binding stay in base?*) rather than pure mechanical ones.

> **Do not skip validation between steps.** Repoclosure cascades — moving
> one SRPM can move other findings into / out of view. After every change
> we re-run the pipeline, eyeball the delta, and only then commit.

## Repo orientation

| Path | Purpose |
| ---- | ------- |
| `base/packages/base.packages.toml`         | Packages published to `rpm-base`. |
| `base/packages/sdk.packages.toml`          | Packages published to `rpm-sdk`. |
| `base/packages/exceptions.packages.toml`   | Curated escape hatch — sub-packages of an otherwise-base SRPM that are intentionally published to `rpm-sdk`. Every entry **must** have a paired allow-list entry. |
| `scripts/srpm-consistency-allowlist.toml`  | Per-SRPM exceptions to the "all binaries from one SRPM publish to the same channel" policy. |
| `base/packages/TODO.md`                    | Deferred-decision log: things we'd like to do but that need a product / policy decision first. |
| `scripts/split-repo-by-channel.py`         | The pipeline: split published repodata by channel, run SRPM-consistency check, run repoclosure × 2, write JSON. |
| `scripts/rebalance-channel.py`             | The mover: `promote` / `demote` / `carve` / `remove` / `analyze`. Use this — do **not** edit the TOMLs by hand. |
| `scripts/repo-split-summary.py`            | One-line summary of the latest pipeline output. |
| `base/build/work/scratch/repo-split/`      | Pipeline output (temp; under workDir). |

## Setup (once per session)

```bash
# Run the pipeline to populate base/build/work/scratch/repo-split/
python3 scripts/split-repo-by-channel.py \
    --output base/build/work/scratch/repo-split \
    2>&1 | tee base/build/work/scratch/split.log >/dev/null
python3 scripts/repo-split-summary.py
```

The summary line gives you the baseline:

```
base      raw=  340 suppressed=   0 remaining=  340
base+sdk  raw=   50 suppressed=   0 remaining=   50
srpm      cross-channel=47 covered=47 uncovered=0
```

The two numbers we drive down are `base.remaining` and `base+sdk.remaining`.
`srpm uncovered` must always remain `0` after each step.

## Workflow (a): produce an actionable candidate table

Run the analyzer:

```bash
python3 scripts/rebalance-channel.py analyze --top 20
```

It clusters every base finding by **consumer SRPM** (which SRPM's binary
RPM has the unresolved dep) and prints a ranked markdown table:

| Rank | Consumer SRPM | Findings | Sub-pkgs leaking | Top provider SRPMs (sdk) | Concrete example |

When presenting the table to the user, **augment** each row (or each row in
the top-N you choose to recommend) with the four columns the user asked
for. Do this in a per-row markdown table, not in prose:

| Column | How to fill it |
| --- | --- |
| **SRPM** | The "Consumer SRPM" cell from the analyzer table. |
| **Action** | One of `promote <provider-srpm>`, `demote <srpm>`, `carve <srpm> <pkg>...`, or `remove <srpm>`. Pick using the heuristics below. |
| **Concrete example** | A real `<consumer-pkg> -> <unresolved-dep>` from the analyzer. Always include both sides; do not reference an SRPM name in the abstract. |
| **Risk** | A short sentence: who could break? What user-facing surface does this SRPM cover? Reverse-dep audit if relevant. |
| **Findings closed (est.)** | The `Findings` count for this SRPM, plus or minus any cascade you can predict. |
| **Rationale** | Why this is a *minimal* / *intentional* move that doesn't bloat base. |

### Heuristics for picking the action

| Pattern | Action |
| --- | --- |
| One sub-package leaks (`Sub-pkgs leaking == 1`) and its name matches `*-qt5*`, `*-kde`, `*-gui`, `*-gtk2`, `*-wx`, `*-mono`, `*-sharp`, `*-monitor`, `*-doc`, `*-docs`, or `python3-*+<extra>` | **carve** — clear policy: optional GUI / language-binding / extras layer goes to sdk |
| The consumer SRPM is build tooling, a desktop install meta, or a niche distro-flavored stack (e.g. ROCm, anaconda installer) | **demote** the whole SRPM |
| A small (≲ 10 sub-pkgs) sdk SRPM dominates the providers column and is genuinely base-tier (auth primitive, fundamental lib) | **promote** that provider SRPM |
| The consumer SRPM is a leaf in *both* channels (no other SRPM requires it) and only its own GUI-binding sub-pkgs are problematic | **remove** the SRPM from the distro entirely (also delete the `[components.<name>]` line in `base/comps/components.toml` and any per-comp dir) |
| The consumer SRPM has *many* leaking sub-packages spread across many providers, and the sdk-side reverse-dep set of the providers is large | **flag in `base/packages/TODO.md`** — needs a policy decision |

### Reverse-dep audits (do them before any whole-SRPM move)

For demote / remove candidates, **always** check what depends on the SRPM
in *both* channels before recommending the move:

```python
# Quick one-liner to enumerate consumers of a capability substring:
python3 -c "
from pathlib import Path
import sys; sys.path.insert(0, 'scripts')
from importlib import import_module
mod = import_module('rebalance-channel'.replace('-', '_'))
" 2>/dev/null  # the script name has a dash; import via spec, see below
```

Easier: just open a python REPL and reuse the helpers from the script:

```bash
python3 - <<'EOF'
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "rc", "scripts/rebalance-channel.py")
rc = importlib.util.module_from_spec(spec); spec.loader.exec_module(rc)
import collections
for ch in ("base", "sdk"):
    edges = rc.consumers_of(rc.DEFAULT_SPLIT_DIR / ch, "ffmpeg")
    print(f"=== {ch} consumers of *ffmpeg* ===")
    for n, k in collections.Counter(p for p, _ in edges).most_common():
        print(f"  {k:3d}  {n}")
EOF
```

If the sdk-side consumer set is large (≳ 5 distinct SRPMs) and includes
broadly-used components (KDE, GNOME, Plasma, etc.), the whole-SRPM
**remove** is almost certainly wrong; recommend a carve or flag in
`TODO.md` instead.

## Workflow (b): apply a user-approved batch

For every action the user approves, follow this loop:

> **Critical**: commit each step separately. The user wants a clean,
> bisectable history of policy decisions, and an unexpected regression in
> step N is much easier to surface and discuss when N is its own commit.

For each candidate action:

1. **Apply** with the appropriate subcommand:

   ```bash
   # Whole-SRPM moves (no allowlist edit needed):
   python3 scripts/rebalance-channel.py promote <srpm>
   python3 scripts/rebalance-channel.py demote  <srpm>

   # Carve specific sub-packages into the exceptions list:
   python3 scripts/rebalance-channel.py carve <srpm> \
       --exception-comment '<one-line WHY for exceptions.packages.toml>' \
       --allowlist-reason  '<one-line WHY for srpm-consistency-allowlist>' \
       <pkg> [<pkg>...]

   # Drop an SRPM from base entirely (still need to manually delete
   # [components.<name>] from base/comps/components.toml):
   python3 scripts/rebalance-channel.py remove <srpm>
   ```

2. **For `remove`**, also delete the `[components.<srpm>]` line from
   `base/comps/components.toml` (and the per-comp dir under
   `base/comps/<srpm>/` if one exists).

3. **Re-validate**:

   ```bash
   python3 scripts/split-repo-by-channel.py \
       --output base/build/work/scratch/repo-split \
       2>&1 | tee base/build/work/scratch/split.log >/dev/null
   python3 scripts/repo-split-summary.py
   ```

4. **Eyeball the delta** vs. the previous step.

   * `base.remaining` should drop by approximately the `Findings` value
     for that SRPM. If it drops less, look at the per-finding cascade.
   * `base+sdk.remaining` should be **unchanged** — if it goes up, you
     just introduced a new sdk-side break and need to investigate.
   * `srpm uncovered` must remain `0`. If non-zero, the carve created an
     unanticipated channel split and you need to extend the allow-list.

5. **If the result is unexpected** (delta significantly off, base+sdk
   regression, sdk consumers cascade), **stop and surface it to the
   user** — do **not** commit. Roll back with `git checkout -- <files>`
   if needed. Show the user:
   * what changed,
   * what was expected,
   * the cascade you found,
   * options (e.g., partial carve instead of full remove).

6. **If the result is expected**, commit:

   ```bash
   git add -A base/packages base/comps/components.toml \
              scripts/srpm-consistency-allowlist.toml
   git commit -m "fix: <verb> <srpm> ...

   <one-paragraph WHY: what the SRPM is, what was leaking, why the move>
   Closes <N> base repoclosure findings.

   Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
   ```

   Commit subjects so far on this branch follow the patterns:

   * `fix: demote <srpm> SRPM to sdk`
   * `fix: promote <srpm> SRPM to base`
   * `fix: carve <pkg> [...] to sdk`        (sub-package carve-out)
   * `fix: remove <srpm> entirely from distro`
   * `docs: defer <srpm> ...; flag in TODO.md`  (when skipping)

7. **Track progress** in the session SQL `batch` table (or equivalent):

   ```sql
   INSERT INTO batch VALUES
     (<n>, '<action>', 'done', <base_remaining>, <basesdk_remaining>,
      '<short-sha>', '<delta-note>');
   ```

## Skipping a candidate

When a candidate's reverse-dep audit reveals it would cause significantly
more dependency damage than it resolves (the user explicitly asked for
this safety check), **skip** the apply step. Instead:

1. Do **not** make the package-list change.
2. Append a section to `base/packages/TODO.md` documenting:
   * what was attempted,
   * what reverse-deps were found (concrete consumer pkg names),
   * the alternatives the user could pursue later.
3. Commit the docs change as `docs: defer <srpm> ...; flag in TODO.md`.

## Things that have bitten us

| Pitfall | Mitigation |
| --- | --- |
| `dnf` cache stale across pipeline runs | `split-repo-by-channel.py` already uses a fresh per-scope cachedir; do not override. |
| Carving without an allowlist entry | The `carve` subcommand always writes both files; do not bypass. |
| Top-meta SRPM (e.g. `anaconda`) `Requires: <gui-subpkg>` — carving the GUI subpkg without also carving the meta creates a new finding | Audit `Requires:` of the SRPM's meta package before deciding the carve set. |
| Removing an SRPM with broad sdk consumers (e.g. `libcanberra`, `ffmpeg`) | Always run a reverse-dep audit; if ≳ 5 sdk consumers, demote or flag instead. |
| Promoting an SRPM that pulls a transitive sdk-only stack | The `promote` subcommand only moves the named SRPM — re-run the pipeline after; if base+sdk is dirty, a transitive promote is needed too. |
| Editing `exceptions.packages.toml` by hand and getting the comment grouping wrong | Always use `carve`; it appends with a one-line comment in the right place. |

## Reference: candidate patterns we have used

* `*-qt5` / `*-qt5-devel` Qt5 binding (avahi, libportal, poppler)
* `*-kde` KDE/Plasma integration (subversion-kde)
* `*-gtk2` legacy GTK2 binding (libcanberra-gtk2)
* `*-wx` wxWidgets-based GUI (erlang-wx)
* `*-monitor` Qt-based GUI for a daemon (thermald-monitor)
* `*-latex` / `*-pdf` LaTeX-based doc backend (doxygen-latex, python3-sphinx-latex)
* `*-doxywizard` / `*-gui` GUI editor for a CLI tool (doxygen-doxywizard)
* `python3-<pkg>+<extra>` PEP-508 extras metapackage (python-jsonschema+format, python-fsspec+dask, python-sqlalchemy+aioodbc)
* `mingw32-<pkg>` / `mingw64-<pkg>` Windows cross-compile artifact
* SRPM whole-demote: build tooling, GPU compute meta, niche language tooling (rocm, dola, maven-doxia, gpsbabel)
* SRPM whole-remove: leaf SRPM with 0 sdk consumers (yggdrasil, libportal)
