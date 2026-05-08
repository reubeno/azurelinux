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

:data:`EXPECTED_MISSING_DEPS` is a ``dict[consumer_name, frozenset
of dep_strings]``. The key is the *consumer* package name (the
package whose Requires is unresolved), unique-ified by name only --
versions and arches are stripped so entries survive version bumps.
Each value is the set of missing dep strings that consumer is
permitted to be missing (each string is exactly what
``libdnf5.RelDep.to_string()`` returns).

Per-failing-NEVRA classification:

* If the consumer's name is *not* in :data:`EXPECTED_MISSING_DEPS`,
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
  dep at all -> "remove the entry".
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

import pytest


# Per-consumer-name allowlist of known-missing runtime deps. Key
# is the consumer's package *name* (no epoch/version/release/arch).
# Value is the frozenset of dep strings that consumer's NEVRAs are
# permitted to be missing in this repo. Add a one-line comment for
# each cluster explaining the underlying gap so the list stays
# curatable.
EXPECTED_MISSING_DEPS: dict[str, frozenset[str]] = {
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

    # ----- GHC http-streams package not yet ported.
    "ghc-haxr":       frozenset({"libHShttp-streams-0.8.9.9-C19TWyOER7n4t8iS4JMjuZ-ghc9.8.4.so()(64bit)"}),
    "ghc-haxr-devel": frozenset({"ghc-devel(http-streams-0.8.9.9-C19TWyOER7n4t8iS4JMjuZ)"}),
    "ghc-haxr-prof":  frozenset({"ghc-prof(http-streams-0.8.9.9-C19TWyOER7n4t8iS4JMjuZ)"}),
    "ghc-koji":       frozenset({"libHShttp-streams-0.8.9.9-C19TWyOER7n4t8iS4JMjuZ-ghc9.8.4.so()(64bit)"}),

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

    # ----- Ruby gems: nokogiri not packaged; one self-pin NEVR
    # mismatch on the -doc subpackage of bundler/rdoc.
    "rubygem-actionpack":           frozenset({"rubygem(nokogiri) >= 1.8.5"}),
    "rubygem-actiontext":           frozenset({"rubygem(nokogiri) >= 1.8.5"}),
    "rubygem-bundler-doc":          frozenset({"rubygem-bundler = 2.6.9-4.azl4"}),
    "rubygem-capybara":             frozenset({"(rubygem(nokogiri) >= 1.11 with rubygem(nokogiri) < 2)"}),
    "rubygem-loofah":               frozenset({"rubygem(nokogiri) >= 1.12.0"}),
    "rubygem-rails-dom-testing":    frozenset({"rubygem(nokogiri) >= 1.6"}),
    "rubygem-rails-html-sanitizer": frozenset({"(rubygem(nokogiri) >= 1.14 with rubygem(nokogiri) < 2)"}),
    "rubygem-rdoc-doc":             frozenset({"rubygem-rdoc = 6.4.0-209.azl4"}),
    "rubygem-ronn-ng":              frozenset({"(rubygem(nokogiri) >= 1 with rubygem(nokogiri) < 2 with rubygem(nokogiri) >= 1.14.3)"}),
    "rubygem-xpath":                frozenset({"(rubygem(nokogiri) >= 1.8 with rubygem(nokogiri) < 2)"}),

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
}


def test_repoclosure_base_plus_sdk(
    arch: str, require_named_repos, repoclosure, subtests
) -> None:
    repos = require_named_repos(["base", "sdk"], kind="binary")
    result = repoclosure(repos, arch)

    # Aggregate observed (consumer_name, dep) pairs so the per-dep
    # stale-entry safety rail can fire even when the consumer is
    # still failing for other reasons.
    observed_deps_by_name: dict[str, set[str]] = {}
    for nevra, missing in result.unresolved.items():
        observed_deps_by_name.setdefault(nevra.name, set()).update(missing)

    # Classify each failing NEVRA: real fail, expected fail (XFAIL),
    # or new-offender (consumer not in EXPECTED_MISSING_DEPS at all).
    real_failures: dict = {}
    expected_failures: dict = {}
    for nevra, missing in result.unresolved.items():
        listed = EXPECTED_MISSING_DEPS.get(nevra.name)
        if listed is not None and set(missing).issubset(listed):
            expected_failures[nevra] = missing
        else:
            real_failures[nevra] = missing

    # Stale-entry safety rails -- both fire as real failures so the
    # dict shrinks as gaps get fixed. Note: parametrized per-arch.
    # If a (consumer, dep) pair is observed only on certain arches,
    # the others will report it stale. Keep that in mind for
    # multi-arch suites.
    stale_consumer_entries: list[str] = sorted(
        name for name in EXPECTED_MISSING_DEPS
        if name not in observed_deps_by_name
    )
    stale_dep_entries: list[tuple[str, str]] = sorted(
        (name, dep)
        for name, listed in EXPECTED_MISSING_DEPS.items()
        if name in observed_deps_by_name
        for dep in listed
        if dep not in observed_deps_by_name[name]
    )

    for nevra in sorted(real_failures, key=str):
        missing = real_failures[nevra]
        repo = result.repos_by_nevra.get(nevra)
        suffix = f" (from {repo!r})" if repo else ""
        listed = EXPECTED_MISSING_DEPS.get(nevra.name, frozenset())
        new_deps = sorted(set(missing) - listed)
        with subtests.test(package=str(nevra), arch=arch):
            if nevra.name not in EXPECTED_MISSING_DEPS:
                pytest.fail(
                    f"{nevra}{suffix} has unresolved runtime dep(s) "
                    f"and the consumer name is not yet listed in "
                    f"EXPECTED_MISSING_DEPS:\n"
                    + "\n".join(f"  - {d}" for d in missing)
                    + f"\n\nAdd a {nevra.name!r} entry to "
                    f"EXPECTED_MISSING_DEPS if intentional."
                )
            pytest.fail(
                f"{nevra}{suffix} has unresolved runtime dep(s):\n"
                + "\n".join(f"  - {d}" for d in missing)
                + f"\n\nNew (un-allowlisted) dep(s) for "
                f"{nevra.name!r} -- extend its EXPECTED_MISSING_DEPS "
                f"entry if intentional:\n"
                + "\n".join(f"  - {d}" for d in new_deps)
            )

    for nevra in sorted(expected_failures, key=str):
        missing = expected_failures[nevra]
        repo = result.repos_by_nevra.get(nevra)
        suffix = f" (from {repo!r})" if repo else ""
        with subtests.test(package=str(nevra), arch=arch):
            pytest.xfail(
                f"known-missing runtime dep(s) (tracked in "
                f"EXPECTED_MISSING_DEPS[{nevra.name!r}]): "
                f"{nevra}{suffix}:\n"
                + "\n".join(f"  - {d}" for d in missing)
            )

    for name in stale_consumer_entries:
        with subtests.test(consumer=name, arch=arch, kind="stale-consumer"):
            pytest.fail(
                f"consumer {name!r} is listed in "
                f"EXPECTED_MISSING_DEPS but no NEVRA of that name "
                f"is reporting unresolved deps on {arch}. Please "
                f"remove the entry."
            )

    for name, dep in stale_dep_entries:
        with subtests.test(consumer=name, missing_dep=dep, arch=arch, kind="stale-dep"):
            pytest.fail(
                f"dep {dep!r} is listed in "
                f"EXPECTED_MISSING_DEPS[{name!r}] but is no longer "
                f"reported as missing for any NEVRA of {name!r} on "
                f"{arch}. Please remove that dep from the entry."
            )
