# TODO — Repo Channel Split & Cleanup

Working notes for the in-flight effort to split the published RPM set into
`base` (fully supported) and `sdk` (built but unsupported), driven by
`scripts/split-repo-by-channel.py` and the per-package `publishChannel`
designations in `base/packages/**/*.toml`.

The script:
1. Loads publish metadata via `azldev`.
2. Splits a snapshot of upstream repodata into per-channel sub-repos.
3. Runs an **SRPM channel-consistency check** — every binary RPM produced
   by a given SRPM must publish to the same channel. Violations are emitted
   to `srpm-consistency.{txt,json}` and (uncovered) violations cause the
   script to exit non-zero. An allowlist lives at
   `scripts/srpm-consistency-allowlist.toml`.
4. Runs `dnf repoclosure` against (a) `base` alone and (b) `base+sdk`,
   emits human-readable and structured JSON reports. An allowlist of
   intentional unresolved deps lives at `scripts/repoclosure-allowlist.toml`.

## Current state

- **Two package lists only:** `base.packages.toml` (rpm-base) and
  `sdk.packages.toml` (rpm-sdk). The previous `base-sibling`, `sdk-sibling`,
  and `todo` groupings have been merged into `sdk-packages`.
- **SRPM-consistency invariant: enforced and currently 0 violations.** Every
  binary RPM produced by a given SRPM publishes to a single channel.
- Latest split (after consistency enforcement): **base = 8016 pkgs**,
  **sdk = 25046 pkgs**.

## Active workstreams

### 1. Repoclosure (`base` scope)
Re-run `dnf repoclosure` against the new `base` and curate the violation
list. Decisions per unresolved dep:
- **Promote** the provider (pulls its full SRPM family into base — beware
  blast radius now that we enforce SRPM consistency).
- **Demote** the consumer (move its full SRPM family out of base).
- **Overlay** the consumer to drop the dep (TOML-based spec overlay).
- **Allowlist** the violation in `scripts/repoclosure-allowlist.toml` with
  a `confidence` rating and reason.

Previously surfaced clusters (need re-validation against the new split):
- Plasma / Qt5 / KF5 desktop stack (deferred during policy work).
- mingw32 / mingw64 cross-compile ecosystem.
- GUI/desktop packages mistakenly in base: `Thunar`, `gvfs`,
  `anaconda-{gui,install-img-deps,live,webui}`,
  `gstreamer1-plugins-bad-free-extras`, `erlang-wx`.

### 2. Curate "elephant" SRPMs in base
Now that SRPM-consistency is enforced, an SRPM with a single intentional
base sub-package implicitly drags every sibling into base. Watch for:
- `ceph` (~47 pkgs), `proj` (~38), `rust-rav1e` (~34), `langpacks` (~29),
  `ocaml-dune` (~27), `erlang` (~23), `java-25-openjdk` (~20),
  `libarrow` (~18), `nbdkit` (~18), `glusterfs` (~15).
Demote candidates → move all base sub-packages of these SRPMs out of
`base.packages.toml`.

### 3. Pre-existing gaps in `base+sdk`
~51 unresolved deps remained even with both channels enabled (snapshot
prior to merges). Re-baseline against the new split, then track:
- `gcr3` family (gnome-keyring, gnome-online-accounts, ...).
- `gnome-shell`, `control-center-filesystem`, `mesa-va-drivers`,
  `javapackages-local`, `fedora-logos*`, `rust-srpm-macros`,
  `python3.14dist(uv)`.
- TeXLive perl modules: `BibTeX::Parser`, `LaTeX::ToUnicode`,
  `BibTeX::Parser::Author`, `WWW::Mechanize`, `Spreadsheet::ParseExcel`,
  `Switch`, plus `R-knitr`, `asymptote`, `pdfpc`, `snobol4`,
  `oldstandard-sfd-fonts`.
- `rubygem-*-doc` packages depending on the underlying gem.

### 4. Tooling improvements
- Promote the cross-reference analysis (resolved-by-sdk vs. truly-missing,
  per-SRPM rollups) into the script itself, rather than running ad-hoc.
- Add a **"blast radius" estimate**: if we demote SRPM X, how many other
  base packages still require any of its sub-packages? (Especially
  important now that SRPM consistency means demotes/promotes are
  all-or-nothing.)
- Consider an `azldev`-native command for the per-channel split + closure.

## Completed

- ✅ Split tooling: `scripts/split-repo-by-channel.py` is self-contained
  (split → repodata → SRPM-consistency → repoclosure × 2 → JSON).
- ✅ SRPM channel-consistency policy adopted and enforced (script exits
  non-zero on uncovered violations).
- ✅ Demoted full SRPM families to `sdk`: `texlive`, `texlive-base`, `ghc`,
  all `ghc-*` (212 SRPMs), `google-noto*` (5), `tesseract*` (2), `pipewire`.
- ✅ Mechanical SRPM alignment: every sub-package of any base-contributing
  SRPM moved into `base.packages.toml`.
- ✅ Collapsed package lists: `base-sibling.packages.toml`,
  `sdk-sibling.packages.toml`, and `todo.packages.toml` merged into
  `sdk.packages.toml`. The repo now has exactly two package lists.

## Conventions for this work

- Verify next actions before taking them. Ask before bulk demote/promote.
- Use the allowlist at `scripts/repoclosure-allowlist.toml` for
  *intentional* unresolved deps with a `confidence` rating.
- Use the allowlist at `scripts/srpm-consistency-allowlist.toml` for
  *intentional* per-SRPM channel exceptions.
- Keep `base+sdk` closure tight; the allowed set there should stay small
  and documented.
