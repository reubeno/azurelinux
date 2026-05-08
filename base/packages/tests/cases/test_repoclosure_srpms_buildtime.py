# SPDX-License-Identifier: MIT
"""Every SRPM in the ``base-srpms`` and ``sdk-srpms`` repos must be
build-time-closed against ``base + sdk`` binary repos.

This asserts that for each source RPM in ``base-srpms ∪ sdk-srpms``,
its ``BuildRequires:`` set is satisfiable by binary providers in
``base ∪ sdk ∪ base-srpms ∪ sdk-srpms``. The two SRPM channels are
checked together because the underlying binary universe is the
same: all of azldev's daily builds (regardless of publish channel)
run against ``base + sdk`` binaries, so build-time closure is a
single property over the union, not a per-channel one.

How this maps to dnf5
---------------------

dnf5 represents an SRPM's ``BuildRequires:`` as ``Requires:`` on the
source-arch package in the SRPM repo's ``primary.xml``. So
``dnf5 repoclosure`` over an SRPM repo, with the binary universe
enabled, naturally checks build-time closure.

Why ``check_kind="buildtime"`` (not "source-only")
--------------------------------------------------

Filtering the checker to source-arch only would silently miss a
critical transitive failure: if an SRPM's BuildRequires *name*
resolves to a binary provider, but that binary provider's own
runtime deps are unsatisfied, the SRPM is still not buildable in
practice. ``buildtime`` includes ``arch``, ``noarch``, ``src``, and
``nosrc`` so both the SRPM-level BuildRequires *and* the runtime
closure of any binary that participates as a provider are checked.

Skip / fail behavior
--------------------

Hard-coded for ``base-srpms`` + ``sdk-srpms`` + ``base`` + ``sdk``.
If any of those ``--repo`` flags are missing, the test fails (see
:func:`require_named_repos` — release-gating closure tests are only
meaningful with the full named repo set, and silently skipping a
release-gating closure check is worse than failing loudly). Use
``pytest -k`` / ``--ignore`` to deselect intentionally.

Baselined missing deps (XFAIL allowlist)
----------------------------------------

:data:`EXPECTED_MISSING_DEPS` is a ``dict[consumer_name, value]``
where ``value`` is either a flat ``frozenset[str]`` of permitted
missing-dep strings (applies on every arch) or a ``dict[arch_name,
frozenset[str]]`` (applies only on the listed arches). The key is
the *consumer* package name (the package whose BuildRequires/
Requires is unresolved), unique-ified by name only — versions and
arches are stripped so entries survive version bumps. Each dep
string is exactly what ``libdnf5.RelDep.to_string()`` returns.

Per-failing-NEVRA classification:

* If the consumer's name is *not* in :data:`EXPECTED_MISSING_DEPS`
  (or the entry is arch-gated and does not list the current arch),
  emit a real failure (new offender).
* Otherwise, if the NEVRA's missing-dep set is a *subset* of the
  listed deps for that consumer, emit ``XFAIL`` for that subtest
  — visible in pytest output and counted toward the xfail tally,
  but does not fail the run.
* If the NEVRA introduces *any* missing dep that is not in the
  listed set for its consumer, emit a real failure and call out
  the new dep(s) (ceiling breach).

Stale-entry safety rails (real failures, to nudge cleanup):

* A listed consumer that is no longer reported with any unresolved
  dep at all → "remove the entry". Arch-gated entries only
  participate on the arches they list, so an aarch64-only entry
  will not be reported as stale on x86_64 (and vice versa).
* A listed consumer that *is* still failing, but a listed dep is
  no longer reported missing for any of its NEVRAs → "remove dep
  X from this entry".

The whole point: each (consumer, dep) pair is explicit. Adding a
new gap requires explicitly listing both sides; fixing one shrinks
the list automatically (via the safety rails firing on the next
run after the gap closes).
"""

from __future__ import annotations

from utils.repoclosure import ExpectedMissingMap, assert_expected_missing


# Per-consumer-name allowlist of known-missing deps. Key is the
# consumer's package *name* (no epoch/version/release/arch). Value
# is either a flat ``frozenset`` of permitted missing-dep strings
# (applies on every arch) or a ``dict[arch_name, frozenset]``
# (applies only on the listed arches). Add a one-line comment for
# each cluster explaining the underlying gap so the list stays
# curatable.
EXPECTED_MISSING_DEPS: ExpectedMissingMap = {
    # ----- Java / maven-local toolchain (no openjdk21 stack).
    "apache-ivy":         frozenset({"ant-openjdk21"}),
    "apache-sshd":        frozenset({"maven-local-openjdk21"}),
    "l10n-maven-plugin":  frozenset({"maven-local"}),
    "libbluray":          frozenset({"ant-openjdk21"}),
    "libidn":             frozenset({"javapackages-local"}),
    "libsvm":             frozenset({"maven-local"}),
    "plexus-velocity":    frozenset({"maven-local-openjdk21"}),
    "qdbm":               frozenset({"java-21-openjdk-devel", "javapackages-local-openjdk21"}),
    "resteasy":           frozenset({"maven-local-openjdk21"}),
    "stringtemplate4":    frozenset({"maven-local-openjdk21"}),
    "xbean":              frozenset({"maven-local-openjdk21"}),

    # ----- Repo-config self-pin: -evergreen subpkg pins exact NEVR
    # of azurelinux-repos which isn't being built/published yet.
    "azurelinux-repos-evergreen": frozenset({"azurelinux-repos = 4.0-8.azl4"}),

    # ----- Old GNOME crypto stack (gcr3/gck) not packaged.
    "cinnamon": frozenset({
        "libgcr-base-3.so.1()(64bit)",
        "pkgconfig(gcr-base-3)",
    }),
    "gnome-keyring": frozenset({
        "gcr3",
        "libgck-1.so.0()(64bit)",
        "libgcr-base-3.so.1()(64bit)",
        "pkgconfig(gcr-3) >= 3.27.90",
    }),
    "gnome-online-accounts": frozenset({
        "libgcr-4.so.4()(64bit)",
        "pkgconfig(gcr-4)",
    }),
    "gnome-online-accounts-libs": frozenset({"libgcr-4.so.4()(64bit)"}),
    "gnome-settings-daemon": frozenset({
        "libgck-2.so.2()(64bit)",
        "libgcr-4.so.4()(64bit)",
        "pkgconfig(gck-2)",
        "pkgconfig(gcr-4)",
    }),
    "gvfs": frozenset({"libgcr-4.so.4()(64bit)", "pkgconfig(gcr-4)"}),
    "libgdata": frozenset({"libgcr-4.so.4()(64bit)", "pkgconfig(gcr-4)"}),
    "libgdata-devel": frozenset({"pkgconfig(gcr-4)"}),
    "libnma": frozenset({
        "gcr-devel",
        "libgck-2.so.2()(64bit)",
        "libgcr-4.so.4()(64bit)",
    }),
    "network-manager-applet": frozenset({"gcr-devel"}),
    "pinentry":        frozenset({"pkgconfig(gcr-4)"}),
    "pinentry-gnome3": frozenset({"libgcr-4.so.4()(64bit)"}),

    # ----- GNOME desktop bits with missing siblings.
    "gnome-session": frozenset({"control-center-filesystem"}),
    "mutter":        frozenset({"control-center-filesystem"}),

    # ----- Erlang OTP optional applications not split out into
    # their own subpackages here.
    "elixir":              frozenset({"erlang-dialyzer", "erlang-doc"}),
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
    "python-build":             frozenset({"python3dist(uv) >= 0.1.18"}),
    "python-django5":           frozenset({"python3dist(psycopg) >= 3.1.8", "python3dist(psycopg-pool) >= 3.2"}),
    "python-geopandas":         frozenset({"python3dist(psycopg) >= 3.1"}),
    "python-sqlalchemy-utils":  frozenset({"python3dist(psycopg) >= 3.1.8"}),
    "python3-bleach+css":       frozenset({"(python3.14dist(tinycss2) < 1.5~~ with python3.14dist(tinycss2) >= 1.1)"}),
    "python3-build+uv":         frozenset({"python3.14dist(uv) >= 0.1.18"}),

    # ----- Ruby gems: selenium browser drivers (chromium/
    # chromedriver) not packaged; one self-pin NEVR mismatch on the
    # -doc subpackages of bundler/rdoc.
    "rubygem-actionpack": frozenset({
        "chromedriver", "chromium", "chromium-headless",
    }),
    "rubygem-actiontext": frozenset({
        "chromedriver", "chromium", "chromium-headless",
    }),
    "rubygem-bundler-doc":   frozenset({"rubygem-bundler = 2.6.9-4.azl4"}),
    "rubygem-rdoc-doc":           frozenset({"rubygem-rdoc = 6.4.0-209.azl4"}),
    "rubygem-selenium-webdriver": frozenset({"chromedriver", "chromium", "chromium-headless"}),

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
    "texlive-bibtexperllibs":    frozenset({"perl(BibTeX::Parser)", "perl(LaTeX::ToUnicode)"}),
    "texlive-collection-binextra": frozenset({"asymptote"}),
    "texlive-crossrefware":      frozenset({
        "perl(BibTeX::Parser)",
        "perl(BibTeX::Parser::Author)",
        "perl(LaTeX::ToUnicode)",
    }),
    "texlive-ctanupload":        frozenset({"perl(WWW::Mechanize)"}),
    "texlive-exceltex":          frozenset({"perl(Spreadsheet::ParseExcel)"}),
    "texlive-includernw":        frozenset({"R-knitr"}),
    "texlive-oldstandard":       frozenset({"oldstandard-sfd-fonts"}),
    "texlive-pdfpc-movie":       frozenset({"pdfpc"}),
    "texlive-texaccents":        frozenset({"/usr/bin/snobol4", "snobol4"}),

    # ----- aarch64-only: ROCm GPU stack is x86_64-only upstream, so
    #       every SRPM that BuildRequires ROCm headers/libs has a gap
    #       on aarch64 (where none of the ROCm RPMs are built).
    "aqlprofile":              {"aarch64": frozenset({"rocm-runtime-devel"})},
    "hipblas": {"aarch64": frozenset({
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocsolver-devel",
    })},
    "hipblaslt": {"aarch64": frozenset({
        "hipblas-devel",
        "hipcc",
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-llvm-devel",
        "rocm-runtime-devel",
        "rocminfo",
        "roctracer-devel",
    })},
    "hipcub": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "hipfft": {"aarch64": frozenset({
        "rocfft-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "hipify":                  {"aarch64": frozenset({"rocm-clang-devel", "rocm-llvm-static"})},
    "hiprand": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocrand-devel",
    })},
    "hipsolver": {"aarch64": frozenset({
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocsolver-devel",
        "rocsparse-devel",
    })},
    "hipsparse": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocsparse-devel",
    })},
    "hipsparselt": {"aarch64": frozenset({
        "hipcc",
        "hipsparse-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-llvm-devel",
        "rocm-runtime-devel",
        "rocminfo",
        "rocsparse-devel",
    })},
    "kokkos": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocthrust-devel",
    })},
    "magma": {"aarch64": frozenset({
        "hipblas-devel",
        "hipsparse-devel",
        "rocm-comgr-devel",
        "rocm-core-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "miopen": {"aarch64": frozenset({
        "hipblas-devel",
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocrand-devel",
        "roctracer-devel",
    })},
    "mivisionx": {"aarch64": frozenset({
        "hipcc",
        "miopen-devel",
        "rocblas-devel",
        "rocm-hip-devel",
        "rocm-omp-devel",
        "rocm-rpp-devel",
        "rocm-runtime-devel",
    })},
    "onnxruntime": {"aarch64": frozenset({
        "hipblas-devel",
        "hipcc",
        "hipcub-devel",
        "hipfft-devel",
        "hipify",
        "hiprand-devel",
        "hipsparse-devel",
        "miopen-devel",
        "rccl-devel",
        "rocblas-devel",
        "rocm-clang",
        "rocm-core-devel",
        "rocm-device-libs",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocthrust-devel",
        "roctracer-devel",
    })},
    "python-torch": {"aarch64": frozenset({
        "hipblas-devel",
        "hipblaslt-devel",
        "hipcub-devel",
        "hipfft-devel",
        "hiprand-devel",
        "hipsolver-devel",
        "hipsparse-devel",
        "hipsparselt-devel",
        "magma-devel",
        "miopen-devel",
        "rccl-devel",
        "rocblas-devel",
        "rocfft-devel",
        "rocm-comgr-devel",
        "rocm-core-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocrand-devel",
        "rocsolver-devel",
        "rocthrust-devel",
        "roctracer-devel",
    })},
    "python3-tensile-devel":   {"aarch64": frozenset({"hipcc", "rocminfo"})},
    "rccl": {"aarch64": frozenset({
        "hipify",
        "rocm-comgr-devel",
        "rocm-core-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocal": {"aarch64": frozenset({
        "mivisionx-devel >= 6.4",
        "rocdecode-devel >= 6.4",
        "rocjpeg-devel >= 6.4",
        "rocm-comgr-devel",
        "rocm-hip-devel >= 6.4",
        "rocm-omp-devel >= 6.4",
        "rocm-rpp-devel >= 6.4",
        "rocm-runtime-devel >= 6.4",
    })},
    "rocalution": {"aarch64": frozenset({
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocrand-devel",
        "rocsparse-devel",
    })},
    "rocblas": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocclr": {"aarch64": frozenset({
        "hipcc",
        "rocm-comgr-devel",
        "rocm-runtime-devel >= 6.4",
        "rocm-runtime-devel >= 6.4.2",
    })},
    "rocdecode": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocfft": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocjpeg": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
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
    "rocm-bandwidth-test":     {"aarch64": frozenset({"rocm-runtime-devel >= 6.4.0"})},
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
    "rocm-examples": {"aarch64": frozenset({
        "hipblas-devel",
        "hipcub-devel",
        "hipfft-devel",
        "hipify",
        "hiprand-devel",
        "hipsolver-devel",
        "rocblas-devel",
        "rocfft-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocsolver-devel",
        "rocsparse-devel",
        "rocthrust-devel",
    })},
    "rocm-omp":                {"aarch64": frozenset({"rocm-device-libs", "rocm-runtime-devel"})},
    "rocm-rpm-macros-modules": {"aarch64": frozenset({"rocm-llvm-filesystem"})},
    "rocm-rpp": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-omp-devel",
        "rocm-runtime-devel",
    })},
    "rocm-runtime":            {"aarch64": frozenset({"rocm-device-libs", "rocm-llvm-static"})},
    "rocm-test":               {"aarch64": frozenset({"kfdtest >= 6.4", "rocm-bandwidth-test >= 6.4"})},
    "rocminfo":                {"aarch64": frozenset({"rocm-runtime-devel >= 6.4.0"})},
    "rocprim": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocrand": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocsolver": {"aarch64": frozenset({
        "rocblas-devel",
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
        "rocsparse-devel",
    })},
    "rocsparse": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocthrust": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "roctracer": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-runtime-devel",
    })},
    "rocwmma": {"aarch64": frozenset({
        "rocm-comgr-devel",
        "rocm-hip-devel",
        "rocm-omp-devel",
        "rocm-runtime-devel",
    })},
    "ucx":                     {"aarch64": frozenset({"rocm-hip-devel"})},

    # ----- aarch64-only: BIOS bootloader (syslinux) is x86-only, and
    #       firmware/network-boot helpers that BuildRequire it inherit
    #       the gap on aarch64.
    "ipxe":                       {"aarch64": frozenset({"syslinux"})},
    "syslinux-extlinux-nonlinux": {"aarch64": frozenset({"syslinux"})},
    "syslinux-nonlinux":          {"aarch64": frozenset({"syslinux"})},

    # ----- aarch64-only: shim BuildRequires the x86-only signing
    #       intermediates (ia32 + x64 unsigned binaries) which only
    #       exist on x86_64.
    "shim": {"aarch64": frozenset({"shim-unsigned-ia32 = 15.8", "shim-unsigned-x64 = 15.8"})},

    # ----- aarch64-only: x86_64 32-bit multilib BuildRequires.
    #       The (glibc32 or glibc-devel(x86-32)) rich-dep is only
    #       satisfiable on x86_64 (where 32-bit glibc multilib lives).
    "gcc":      {"aarch64": frozenset({"(glibc32 or glibc-devel(x86-32))"})},
    "gnu-efi":  {"aarch64": frozenset({"(glibc-devel(x86-32) or glibc32)"})},
    "mold":     {"aarch64": frozenset({"(glibc32 or glibc-devel(x86-32))"})},
    "syslinux": {"aarch64": frozenset({"(glibc-devel(x86-32) or glibc32)"})},

    # ----- aarch64-only: Fortran / quad-precision bits packaged only
    #       on x86_64 upstream (libquadmath-devel, gcc-gfortran(x86-64)
    #       multilib provider, and lfortran).
    "boost":    {"aarch64": frozenset({"libquadmath-devel"})},
    "mysql8.4": {"aarch64": frozenset({"libquadmath-devel"})},
    "papilo":   {"aarch64": frozenset({"libquadmath-devel"})},
    "soplex":   {"aarch64": frozenset({"libquadmath-devel"})},
    "tlfloat":  {"aarch64": frozenset({"libquadmath-devel"})},
    "sundials": {"aarch64": frozenset({"gcc-gfortran(x86-64)"})},
    "sympy":    {"aarch64": frozenset({"lfortran"})},

    # ----- aarch64-only: Intel x86 acceleration libraries (QAT, VPL,
    #       libvmaf, PMDK, PSM2, Intel Processor Trace) are upstream
    #       x86_64-only, so SRPMs that opt in to them BuildRequire
    #       providers that don't exist on aarch64.
    "aom":                         {"aarch64": frozenset({"pkgconfig(libvmaf)"})},
    "ceph":                        {"aarch64": frozenset({"qatlib-devel", "qatzip-devel"})},
    "erofs-utils":                 {"aarch64": frozenset({"pkgconfig(qpl) >= 1.5.0"})},
    "ffmpeg":                      {"aarch64": frozenset({"pkgconfig(libvmaf)", "pkgconfig(vpl) >= 2.6"})},
    "fio":                         {"aarch64": frozenset({"libpmem-devel"})},
    "gdb":                         {"aarch64": frozenset({"libipt-devel"})},
    "gstreamer1-plugins-bad-free": {"aarch64": frozenset({"pkgconfig(vpl) >= 2.2"})},
    "libfabric":                   {"aarch64": frozenset({"libpsm2-devel"})},
    "mpich":                       {"aarch64": frozenset({"libpsm2-devel"})},
    "opencv":                      {"aarch64": frozenset({"libvpl-devel"})},
    "openmpi":                     {"aarch64": frozenset({"libpsm2-devel"})},
    "qat-zstd-plugin":             {"aarch64": frozenset({"qatlib-devel"})},
    "qatengine": {"aarch64": frozenset({
        "intel-ipp-crypto-mb-devel >= 1.0.6",
        "intel-ipsec-mb-devel >= 2.0",
        "qatlib-devel >= 23.02.0",
    })},
    "qatzip":                      {"aarch64": frozenset({"qatlib-devel >= 23.08.0"})},
    "qemu":                        {"aarch64": frozenset({"libpmem-devel", "qatzip-devel"})},
}


def test_repoclosure_srpms_buildtime(
    arch: str, require_named_repos, repoclosure, subtests
) -> None:
    srpms = require_named_repos(["base-srpms", "sdk-srpms"], kind="srpm")
    binaries = require_named_repos(["base", "sdk"], kind="binary")
    result = repoclosure(
        target_repos=srpms,
        arch=arch,
        universe_repos=srpms + binaries,
        check_kind="buildtime",
    )
    assert_expected_missing(
        result, arch, EXPECTED_MISSING_DEPS,
        subtests=subtests, dep_kind="dep",
    )
