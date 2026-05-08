# SPDX-License-Identifier: MIT
"""``base`` and ``sdk`` together must be closed over runtime dependencies.

This test is hard-coded for the (``base`` + ``sdk``) combination. It
runs once per architecture.

Behavior:

* If both ``--repo name=base,...`` and ``--repo name=sdk,...`` were
  provided, run repoclosure with the union as the universe.
* If either is missing, fail loudly. Hard-coded closure tests are
  only meaningful with the full set of named repos provided;
  silently skipping a release-gating check is worse than failing.
  Use ``pytest -k`` / ``--ignore`` to deselect intentionally.

Baselined missing deps (XFAIL allowlist)
----------------------------------------

:data:`EXPECTED_MISSING_DEPS` is a ``dict[consumer_name, value]``
where ``value`` is either a flat ``frozenset[str]`` of permitted
missing-dep strings (applies on every arch) or a ``dict[arch_name,
frozenset[str]]`` (applies only on the listed arches). The key is
the *consumer* package name (the package whose Requires is
unresolved), unique-ified by name only -- versions and arches are
stripped so entries survive version bumps. Each dep string is
exactly what ``libdnf5.RelDep.to_string()`` returns.

Per-failing-NEVRA classification:

* If the consumer's name is *not* in :data:`EXPECTED_MISSING_DEPS`
  (or the entry is arch-gated and does not list the current arch),
  emit a real failure (new offender).
* Otherwise, if the NEVRA's missing-dep set is a *subset* of the
  listed deps for that consumer, emit ``XFAIL`` for that subtest --
  visible in pytest output, counted toward the xfail tally, but
  does not fail the run.
* If the NEVRA introduces *any* missing dep that is not in the
  listed set for its consumer, emit a real failure and call out
  the new dep(s) (ceiling breach).

Stale-entry safety rails (real failures, to nudge cleanup):

* A listed consumer that is no longer reported with any unresolved
  dep at all -> "remove the entry". Arch-gated entries only
  participate on the arches they list, so an aarch64-only entry
  will not be reported as stale on x86_64 (and vice versa).
* A listed consumer that *is* still failing, but a listed dep is
  no longer reported missing for any of its NEVRAs -> "remove
  dep X from this entry".

The whole point: each (consumer, dep) pair is explicit. Adding a
new gap requires explicitly listing both sides; fixing one shrinks
the list automatically (via the safety rails firing on the next
run after the gap closes).

This list is the *runtime*-closure baseline. Many of the same gaps
also surface (with extra entries) in
``test_repoclosure_srpms_buildtime.py``'s build-time
baseline; the two are intentionally independent so that fixing a
runtime gap can land without coordinating a buildtime change.
"""

from __future__ import annotations

from utils.repoclosure import ExpectedMissingMap, assert_expected_missing


# Per-consumer-name allowlist of known-missing runtime deps. Key
# is the consumer's package *name* (no epoch/version/release/arch).
# Value is either a flat ``frozenset`` of permitted missing-dep
# strings (applies on every arch) or a ``dict[arch_name, frozenset]``
# (applies only on the listed arches). Add a one-line comment for
# each cluster explaining the underlying gap so the list stays
# curatable.
EXPECTED_MISSING_DEPS: ExpectedMissingMap = {
    # ----- Repo-config self-pin: -evergreen subpkg pins exact NEVR
    # of azurelinux-repos which isn't being built/published yet.
    "azurelinux-repos-evergreen": frozenset({"azurelinux-repos = 4.0-8.azl4"}),

    # ----- Old GNOME crypto stack (gcr3/gck) not packaged.
    "cinnamon":                   frozenset({"libgcr-base-3.so.1()(64bit)"}),
    "gnome-keyring": frozenset({
        "gcr3",
        "libgck-1.so.0()(64bit)",
        "libgcr-base-3.so.1()(64bit)",
    }),
    "gnome-online-accounts":      frozenset({"libgcr-4.so.4()(64bit)"}),
    "gnome-online-accounts-libs": frozenset({"libgcr-4.so.4()(64bit)"}),
    "gnome-settings-daemon": frozenset({
        "libgck-2.so.2()(64bit)",
        "libgcr-4.so.4()(64bit)",
    }),
    "gvfs":            frozenset({"libgcr-4.so.4()(64bit)"}),
    "libgdata":        frozenset({"libgcr-4.so.4()(64bit)"}),
    "libgdata-devel":  frozenset({"pkgconfig(gcr-4)"}),
    "libnma":          frozenset({"libgck-2.so.2()(64bit)", "libgcr-4.so.4()(64bit)"}),
    "pinentry-gnome3": frozenset({"libgcr-4.so.4()(64bit)"}),

    # ----- GNOME desktop bits with missing siblings.
    "gnome-session": frozenset({"control-center-filesystem"}),
    "mutter":        frozenset({"control-center-filesystem"}),

    # ----- Erlang OTP optional applications not split out into
    # their own subpackages here.
    "erlang-cth_readable": frozenset({"erlang-common_test"}),
    "erlang-lager":        frozenset({"erlang-common_test"}),
    "erlang-rebar3":       frozenset({"erlang-common_test", "erlang-dialyzer"}),

    # ----- KDE / sound-server boolean weak deps not satisfiable.
    "kde-settings-pulseaudio": frozenset({"(alsa-plugins-pulseaudio if pulseaudio)"}),
    "plasma-workspace":        frozenset({"(uresourced if systemd-oomd-defaults)"}),

    # ----- IBus input-method engines (graphical-login conditional)
    # not packaged for these langpacks.
    "langpacks-core-as":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-bn":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-bo":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-gu":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-hi":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ja":    frozenset({"(ibus-anthy if service(graphical-login))"}),
    "langpacks-core-kn":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ko":    frozenset({"(ibus-hangul if service(graphical-login))"}),
    "langpacks-core-mai":   frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ml":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-mni":   frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-mr":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ne":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-or":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-pa":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-sat":   frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-si":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ta":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-te":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-th":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-ur":    frozenset({"(ibus-m17n if service(graphical-login))"}),
    "langpacks-core-vi":    frozenset({"(ibus-unikey if service(graphical-login))"}),
    "langpacks-core-zh_CN": frozenset({"(ibus-libpinyin if service(graphical-login))"}),
    "langpacks-core-zh_HK": frozenset({"(ibus-table-chinese-cangjie if service(graphical-login))"}),
    "langpacks-core-zh_TW": frozenset({"(ibus-chewing if service(graphical-login))"}),

    # ----- Python distribution packages with no provider.
    "python3-bleach+css": frozenset({"(python3.14dist(tinycss2) < 1.5~~ with python3.14dist(tinycss2) >= 1.1)"}),
    "python3-build+uv":   frozenset({"python3.14dist(uv) >= 0.1.18"}),

    # ----- Self-pin NEVR mismatch on the -doc subpackages of
    # bundler/rdoc (their main package is on a newer NEVR than the
    # -doc pin tracks).
    "rubygem-bundler-doc":          frozenset({"rubygem-bundler = 2.6.9-4.azl4"}),
    "rubygem-rdoc-doc":             frozenset({"rubygem-rdoc = 6.4.0-209.azl4"}),

    # ----- Rust crate version gaps in transitive subpackages.
    "rust-ambient-id+astral-reqwest-middleware-devel": frozenset({
        "(crate(astral-reqwest-middleware/default) >= 0.4.2 with crate(astral-reqwest-middleware/default) < 0.5.0~)",
    }),
    "rust-clap_derive+unstable-markdown-devel": frozenset({
        "(crate(pulldown-cmark) >= 0.13.1 with crate(pulldown-cmark) < 0.14.0~)",
    }),
    "rust-io-lifetimes1+socket2-devel": frozenset({
        "(crate(socket2/default) >= 0.4.0 with crate(socket2/default) < 0.5.0~)",
    }),
    "rust-kvm-bindings0.11+vmm-sys-util-devel": frozenset({
        "(crate(vmm-sys-util/default) >= 0.12.1 with crate(vmm-sys-util/default) < 0.13.0~)",
    }),
    "rust-lscolors+crossterm-devel": frozenset({
        "(crate(crossterm/default) >= 0.28.0 with crate(crossterm/default) < 0.29.0~)",
    }),

    # ----- TeX Live add-ons depending on tools/fonts not packaged.
    "texlive-bibtexperllibs":      frozenset({"perl(BibTeX::Parser)", "perl(LaTeX::ToUnicode)"}),
    "texlive-collection-binextra": frozenset({"asymptote"}),
    "texlive-crossrefware":        frozenset({
        "perl(BibTeX::Parser)",
        "perl(BibTeX::Parser::Author)",
        "perl(LaTeX::ToUnicode)",
    }),
    "texlive-ctanupload":  frozenset({"perl(WWW::Mechanize)"}),
    "texlive-exceltex":    frozenset({"perl(Spreadsheet::ParseExcel)"}),
    "texlive-includernw":  frozenset({"R-knitr"}),
    "texlive-oldstandard": frozenset({"oldstandard-sfd-fonts"}),
    "texlive-pdfpc-movie": frozenset({"pdfpc"}),
    "texlive-texaccents":  frozenset({"/usr/bin/snobol4", "snobol4"}),

    # ----- aarch64-only: ROCm GPU stack is x86_64-only upstream, so the
    #       umbrella metapackages have Requires that can't be satisfied
    #       on aarch64 (where none of the ROCm RPMs are built).
    "python3-tensile-devel":   {"aarch64": frozenset({"hipcc", "rocminfo"})},
    "rocm-rpm-macros-modules": {"aarch64": frozenset({"rocm-llvm-filesystem"})},
    "rocm-test":               {"aarch64": frozenset({
        "kfdtest >= 6.4",
        "rocm-bandwidth-test >= 6.4",
    })},
    "rocm": {"aarch64": frozenset({
        "amdsmi >= 6.4",
        "aqlprofile",
        "hipblas >= 6.4",
        "hipblaslt >= 6.4",
        "hipcc",
        "hipfft >= 6.4",
        "hiprand >= 6.4",
        "hipsolver >= 6.4",
        "hipsparse >= 6.4",
        "hipsparselt >= 6.4",
        "miopen >= 6.4",
        "mivisionx >= 6.4",
        "rccl >= 6.4",
        "rocal >= 6.4",
        "rocalution >= 6.4",
        "rocblas >= 6.4",
        "rocdecode >= 6.4",
        "rocfft >= 6.4",
        "rocjpeg >= 6.4",
        "rocm-clang",
        "rocm-clinfo >= 6.4",
        "rocm-core >= 6.4",
        "rocm-hip >= 6.4",
        "rocm-omp >= 6.4",
        "rocm-opencl >= 6.4",
        "rocm-rpp >= 6.4",
        "rocm-runtime >= 6.4",
        "rocminfo >= 6.4",
        "rocrand >= 6.4",
        "rocsolver >= 6.4",
        "rocsparse >= 6.4",
        "roctracer >= 6.4",
    })},
    "rocm-devel": {"aarch64": frozenset({
        "amdsmi-devel >= 6.4",
        "aqlprofile-devel",
        "hipblas-devel >= 6.4",
        "hipblaslt-devel >= 6.4",
        "hipcub-devel >= 6.4",
        "hipfft-devel >= 6.4",
        "hipify >= 6.4",
        "hiprand-devel >= 6.4",
        "hipsolver-devel >= 6.4",
        "hipsparse-devel >= 6.4",
        "hipsparselt-devel >= 6.4",
        "miopen-devel >= 6.4",
        "mivisionx-devel >= 6.4",
        "rccl-devel >= 6.4",
        "rocal-devel >= 6.4",
        "rocalution-devel >= 6.4",
        "rocblas-devel >= 6.4",
        "rocdecode-devel >= 6.4",
        "rocfft-devel >= 6.4",
        "rocjpeg-devel >= 6.4",
        "rocm-clang-devel",
        "rocm-core-devel >= 6.4",
        "rocm-examples >= 6.4",
        "rocm-hip-devel >= 6.4",
        "rocm-omp-static >= 6.4",
        "rocm-opencl-devel >= 6.4",
        "rocm-rpp-devel >= 6.4",
        "rocm-runtime-devel >= 6.4",
        "rocrand-devel >= 6.4",
        "rocsolver-devel >= 6.4",
        "rocsparse-devel >= 6.4",
        "rocthrust-devel >= 6.4",
        "roctracer-devel >= 6.4",
        "rocwmma-devel >= 6.4",
    })},

    # ----- aarch64-only: BIOS bootloader is x86-only. The noarch
    #       *-nonlinux subpackages are published to all channels but
    #       Require `syslinux`, which only builds on x86_64.
    "syslinux-extlinux-nonlinux": {"aarch64": frozenset({"syslinux"})},
    "syslinux-nonlinux":          {"aarch64": frozenset({"syslinux"})},
}


def test_repoclosure_base_plus_sdk(
    arch: str, require_named_repos, repoclosure, subtests
) -> None:
    repos = require_named_repos(["base", "sdk"], kind="binary")
    result = repoclosure(repos, arch)
    assert_expected_missing(
        result, arch, EXPECTED_MISSING_DEPS,
        subtests=subtests, dep_kind="runtime dep",
    )
