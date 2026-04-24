# Package list TODOs

## Revisit fonts & langpacks

`langpacks` (372 sub-pkgs) lives in `rpm-base` today and pulls a large set of
font dependencies — that's why `google-noto-fonts` (317 sub-pkgs) was promoted
to base wholesale. Both are arguably oversized for a server-focused base.

Options to evaluate:

- **Demote `langpacks` to `rpm-sdk`.** It is a Fedora convenience layer of
  ~400 metapackages (`default-fonts-<lang>`, `langpacks-<lang>`, …) for
  desktop/workstation use; servers don't need them. Doing so would also let us
  demote most of `google-noto-fonts` back to sdk. Note: this is a single-SRPM
  demotion (no allowlist needed).
- **Carve out a small base-supported language set.** Keep en/zh/ja/etc. in
  base via an exception list, demote the long tail to sdk. More work; needs a
  product/policy decision on which locales we commit to supporting.
- **Slim the noto promotion.** If langpacks stays, consider only promoting
  the noto sub-pkgs that langpacks actually requires (rather than the full
  317). Trade-off: drift between base and the upstream `google-noto-fonts`
  SRPM (would need an allowlist entry).

Current state (recorded for context):
- `langpacks` SRPM → 372 pkgs in base
- `google-noto-fonts` SRPM → 317 pkgs in base (whole-SRPM promotion)
- `google-noto-sans-cjk-vf-fonts`, `google-noto-serif-cjk-vf-fonts`,
  `google-noto-emoji-fonts`, `stix-fonts`, `fontawesome-fonts` SRPMs (11 sub-pkgs
  total) promoted to base purely to satisfy `langpacks` font-meta Requires
  (`default-fonts-cjk-*`, `langpacks-fonts-{ja,ko,zh_CN,zh_HK,zh_TW}`,
  `default-fonts-core-emoji`, `default-fonts-core-math`, `python-networkx-doc`).
  No external base consumer pulls these via Requires/Recommends, so they only
  enter an image when an image manifest explicitly opts in to one of the locale
  meta-packages — but that means a CJK Noto Variable Font (~30-80 MB each)
  lands per-locale once requested. Consider whether the langpack-fonts metas
  themselves belong in base for a headless distro, which would let us demote
  these fonts back to sdk.

## Revisit OCaml in base

`ocaml-dune` (the OCaml build system, 29 sub-pkgs) lives in `rpm-base`, which
in turn pulled `ocaml-pp` and `ocaml-csexp` (4 sub-pkgs total) into base via
runtime deps. Build tools normally live in `rpm-sdk` (cf. cargo, mvn, gradle).

Options to evaluate:

- **Demote `ocaml-dune` to `rpm-sdk`.** Aligns with the build-tools-in-sdk
  policy and would let us demote `ocaml-pp` / `ocaml-csexp` back too. Need
  to first audit reverse-deps in base — anything else relying on dune
  sub-pkgs would have to be demoted along with it.
- **Keep as-is.** Accept OCaml dune + its 2 small dep SRPMs in base if there
  is a concrete base consumer we want to support.

## Revisit ffmpeg removal

`ffmpeg` (`ffmpeg-free` SRPM, 18 sub-pkgs incl. `libavcodec-free`,
`libavformat-free`, etc.) was demoted from `rpm-base` to `rpm-sdk` to close 22
base repoclosure violations. The original intent was to remove ffmpeg from
the distro entirely, but doing so cascades: many *other* packages in both base
and sdk runtime-link against `libav*` / `libsw*` / `libpostproc`. Concretely,
removal would create:

- 26 new base unresolveds in `chromaprint` (10), `qt6-qtmultimedia` (10), and
  `qt6-qtwebengine` (6).
- 100+ new base+sdk unresolveds across `chromium`, `notcurses`, `neatvnc`,
  `tigervnc`, `kpipewire`, `libvncserver`, `opencv`, `xine-lib`,
  `kf6-kfilemetadata`, …

Options to evaluate:

- **Full removal.** Demote/remove the cascading consumers (`chromaprint`,
  `qt6-qtmultimedia`, `qt6-qtwebengine` from base; the long sdk-side tail can
  follow). Bigger surgery; needs a separate pass.
- **Keep in sdk only (current state).** Pragmatic compromise — base does not
  publish ffmpeg, but it remains available for sdk consumers.

## Revisit libcanberra removal

`libcanberra` (5 sub-pkgs) is the legacy XDG sound-event library, deprecated
upstream in favour of `gsound`. The intent was to remove it entirely from the
distro, but a reverse-dep audit shows ~18 distinct `rpm-sdk` consumers
(`kf5-knotifications`, `kf6-knotifications`, `kf6-knotifyconfig`, `mutter`,
`muffin`, `marco-libs`, `kwin`, `plasma-desktop`, `plasma-workspace`,
`cinnamon-session`, `cinnamon-settings-daemon`, `mate-control-center`,
`mate-settings-daemon`, `gnome-settings-daemon`, `evolution-data-server`,
`gsound`, `pipewire-module-x11`, `vim-X11` in base). Removing the SRPM would
break all of those.

Options to evaluate:

- **Carve out the GTK2 binding only.** `libcanberra-gtk2` and
  `libcanberra-devel` pull `gtk2` (sdk) and would close 5 base findings.
  Core libcanberra + gtk3 binding stay in base. Low risk.
- **Wait for upstream migration.** As consumers migrate to `gsound`, the set
  of dependants will shrink and removal becomes feasible.

## Revisit ffado / jack-audio-connection-kit demotion

`ffado` (FireWire pro-audio) and `jack-audio-connection-kit` (the JACK audio
routing daemon) live in `rpm-base` today. Both are pro-audio user-space
components that are not appropriate for a headless server-focused base
channel. The intent was to demote both to `rpm-sdk`, but doing so cascades
into the qemu-system-* set:

- `libffado` SRPM (3 sub-pkgs) → `jack-audio-connection-kit` hard-Requires
  `libffado.so.2()`, so demoting ffado forces JACK to follow.
- `jack-audio-connection-kit` SRPM (3 sub-pkgs) → demoting JACK orphans
  three jack-backend audio plugins (`mpg123-plugins-jack`,
  `pulseaudio-module-jack`, `qemu-audio-jack`).
- `qemu-audio-jack` is one of the eight audio backends that **every**
  `qemu-system-*` (20 emulators) hard-Requires with versioned exact-version
  deps, so demoting it cascades to all 20 emulators (same pattern that
  blocked the `qemu-audio-pipewire` carve earlier).

Options to evaluate:

- **Demote everything together.** ffado + jack + 3 plugins + 20
  qemu-system-* emulators. Large blast radius; needs a product call on
  whether full-system emulation belongs in base.
- **Add comp.toml overlays to drop `libjack`/`libffado` Requires** from the
  audio plugins (qemu-audio-jack in particular) so the audio backend
  becomes optional/dlopen-style. Needs upstream feasibility check —
  qemu-audio-jack actually links libjack at build time, so this likely
  requires patching the spec rather than just dropping a Requires.
- **Keep as-is.** Accept ffado + JACK in base as the cost of having the
  qemu emulator set in base.

## Revisit apt demotion (and openscap apt-libs dep)

`apt` (the Debian/Ubuntu package manager, 6 sub-pkgs incl. `apt`,
`apt-libs`, `apt-utils`, `apt-devel`) lives in `rpm-base` today. It is for
non-RPM systems (it manipulates `.deb` archives + APT repositories) and
has no business being in a server-focused RPM-native base channel
alongside the equally non-native `dpkg` SRPM (which already lives in
sdk). The intent is to demote the entire apt SRPM to `rpm-sdk`.

Blocker: `openscap` in base hard-Requires `apt-libs` so its OVAL/CVE
scanner can probe `.deb` metadata when scanning a remote Debian/Ubuntu
target. The other 8 openscap sub-pkgs (`openscap-scanner`, `-utils`,
`-libs` via `openscap`, `-engine-sce`, `-python3`, `-perl`, etc.) are
the ones we actually want in base for RPM-system compliance scanning.

Plan:

- **Rebuild openscap with `--without apt`** (i.e. add a `bcond_with apt`
  to the openscap spec via overlay, default off in AZL). Upstream
  openscap already conditionalizes the apt probe; we just need to flip
  the build flag so the apt-libs link goes away.
- Once openscap no longer pulls `apt-libs`, demote the entire apt SRPM
  to sdk via `rebalance-channel.py demote apt apt-apidoc apt-devel apt-doc apt-libs apt-utils`.
- No allowlist entry needed (whole-SRPM demote).

## Revisit gpsd removal

`gpsd` (the GPS daemon and its bindings) was demoted from base to sdk in
this pass — it is hardware-specific (consumer GPS receivers, AIS) and not
appropriate for a server-focused production-supported channel.

The longer-term intent is to **remove gpsd entirely from the distro**.
Defer pending:

- A reverse-dep audit on the sdk side (`consumers_of gpsd-libs` etc.) to
  confirm no widely-used sdk consumer pulls it. A quick scan suggests
  small consumers like `gpsbabel` (already demoted) and a few mapping
  tools — likely removable.
- Confirmation that no Azure Linux image / appliance pulls `gpsd*`.

Once those are clear, drop the SRPM via `rebalance-channel.py remove gpsd`
and delete `[components.gpsd]` from `base/comps/components.toml`.
