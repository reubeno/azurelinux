# Architecture

This document describes the design of the Azure Linux RPM repo
validation tests. It targets contributors who are adding new tests,
debugging existing ones, or extending the framework.

For *user-facing* documentation (how to run the tests, CLI options,
examples), see [`../README.md`](../README.md). For the catalogue of
existing tests and the recipe for adding new ones, see
[`tests.md`](tests.md).

## Goals

1. **Tests are declarative.** A test reads as a clear assertion about
   a published repo. It does not shell out to `dnf`, parse XML, or
   manage caches.
2. **Tests fan out cleanly.** A single test definition becomes one
   pytest result per `(repo, arch)` pair it applies to, with stable
   `repo-arch` ids so failures are easy to triage.
3. **Tests apply to the right repos.** Markers
   (`@pytest.mark.repo_kind(...)` / `@pytest.mark.repo_name(...)`)
   declare what the test is about; non-matching repos do not generate
   test instances at all.
4. **Backends are pluggable but narrow.** The only operation that
   meaningfully needs `dnf5` is `repoclosure`. Everything else is
   metadata-only and is implemented by parsing repodata directly,
   which keeps tests fast and decouples them from `dnf` version
   quirks.

## Layered design

```
                    ┌─────────────────────────────────────────────┐
   tests in cases/  │  test functions (pytest fixtures only)      │
                    └────────────────────────┬────────────────────┘
                                             │ uses
                    ┌────────────────────────▼────────────────────┐
   conftest.py      │  fixtures (repo, arch, repo_packages, ...)  │
                    └────────────────────────┬────────────────────┘
                                             │ calls into
                    ┌────────────────────────▼────────────────────┐
   utils/           │  MetadataService           Repoclosure      │
   (service)        │  (cache + parse)           (in-process)     │
                    └────────────┬───────────────────┬────────────┘
                                 │                   │
                    ┌────────────▼─────────┐  ┌──────▼─────────────┐
   utils/           │  repodata.py         │  │  repoclosure.py    │
   (implementation) │  librepo + createrepo│  │  libdnf5 (libsolv) │
                    └──────────────────────┘  └────────────────────┘
```

**Tests never reach below the fixture layer.** If you find yourself
wanting to import `utils.repodata` or `utils.repoclosure` from a test
file, that's a signal to extend `MetadataService` (or add a fixture)
instead.

The implementation layer is intentionally a thin shim over the
canonical dnf-stack libraries:

* **`librepo`** (`python3-librepo`) — fetches `repomd.xml`, primary,
  and filelists into a per-repo cache directory, plus per-package
  RPMs on demand; verifies checksums; decompresses zchunk / zstd /
  xz / gz transparently; substitutes `$basearch` / `$releasever` in
  URLs.
* **`createrepo_c`** (PyPI; pure-C bindings) — parses primary and
  filelists into typed `Package` objects via libxml2 (streaming).
* **`libdnf5`** (`python3-libdnf5`, libsolv bindings) — loads the
  fetched metadata into a Base/repo_sack and evaluates rich-dep
  requirements via `pool_satisfieddep_map` (the same call
  `dnf5 repoclosure` makes). Rich expressions like `(foo if bar)`,
  `(foo with bar)`, `(foo unless bar)` are evaluated as boolean
  conditionals over the available providers — no special handling
  in our code.
* **`rpm`** (`python3-rpm`, librpm bindings) — reads per-file
  metadata (mode, owner, group, size, digest, linkto) out of
  downloaded RPM headers. Used by the cross-repo file-conflicts
  test to mirror RPM's own `rpmfilesCompare` rules; the createrepo
  XML schema and libdnf5's `Package.get_files` only carry the
  path / type / digest subset.

All four are the same libraries `dnf` itself uses internally, so
the suite's metadata interpretation is guaranteed to match dnf's
without any subprocess shell-out.

## The CLI surface

Defined entirely in [`utils/pytest_plugin.py`](../utils/pytest_plugin.py).
The plugin is registered as a `pytest11` entry point in
[`pyproject.toml`](../pyproject.toml) so its options are known to
pytest *before* it parses argv. This matters because:

* `--workdir` takes a path; without early registration, pytest's
  rootdir logic could mistake the value for a positional test path.
* `--repo` values are arbitrary strings (URLs, names with hyphens, ...).

The plugin parses every `--repo` flag at `pytest_configure` time into
a list of [`Repo`](../utils/repos.py) dataclasses. Validation errors
become a single `pytest.UsageError` with a helpful message; we never
let pytest get to test collection with bad config.

### `--repo` / `--repos-file` syntax

Two equivalent input forms:

```
--repo name=...,kind=...,url=...
```

Strict `key=value` segments separated by commas. The first `=` in each
segment separates key from value, so values containing `=` are
tolerated. Values containing commas are not supported — if a real-world
URL ever needs commas, swap to a URL-encoded form or use the file form
below. URL placeholders like `$basearch` and `$releasever` are
substituted by `librepo` at fetch time.

```
--repos-file path.repo
```

A standard yum/dnf-style ini file, parsed with stdlib `configparser`.
Each section is one repo; the section name is the repo name, `baseurl=`
is the URL, and a custom `kind=` key (`binary` / `srpm` / `debuginfo`)
is required (since the dnf format has no equivalent). May be repeated;
freely combinable with `--repo`. Repo names must be globally unique
across all inputs.

## Test fan-out

Implemented in `pytest_generate_tests` in
[`utils/pytest_plugin.py`](../utils/pytest_plugin.py). The rules:

* If a test has both `repo` and `arch` parameters, parametrize over
  every matching `(repo, arch)` pair (cross product), with ids like
  `base-x86_64`.
* If a test has only `repo`, parametrize over matching repos.
* If a test has only `arch`, parametrize over arches.
* If `repo_kind` / `repo_name` markers eliminate every candidate
  repo, the test is parametrized with a single skipped entry whose
  reason names the missing kind/name. The test does *not* silently
  disappear; it shows up as a skip in pytest output, which is loud
  enough to spot in CI but doesn't break "validate only my SRPM repo"
  workflows.

## Fixture surface

Defined in [`../conftest.py`](../conftest.py). This is what tests
actually consume:

| Fixture | Scope | Returns | Used by |
| --- | --- | --- | --- |
| `repo` | function (parametrized) | one `Repo` after marker filtering | per-repo tests |
| `arch` | function (parametrized) | `str` | every repo-touching test |
| `binary_repos`, `srpm_repos`, `debuginfo_repos` | session | `list[Repo]` | cross-repo tests |
| `releasever` | session | `str | None` | rarely used directly |
| `all_repos` | session | `list[Repo]` | rarely used directly |
| `repo_packages(repo, arch)` | function | `list[Package]` | metadata-only per-repo tests |
| `all_binary_packages(arch)` | function | `dict[Repo, list[Package]]` | cross-repo metadata tests |
| `cross_repo_file_index(arch)` | function | `dict[path, list[FileOwner]]` | the file-conflicts test (first-pass overlap discovery) |
| `package_file_metadata(arch, nevra)` | function | `dict[path, FileMeta]` | the file-conflicts test (second-pass `rpmfilesCompare`) |
| `repoclosure(target_repos, arch)` | function | `RepoclosureResult` | the repoclosure tests |
| `require_named_repos(names, kind=...)` | function | `list[Repo]` | tests with hard-coded repo expectations |

> **`require_named_repos` semantics.** Tests that use this fixture
> declare "I cannot pass without all of these specific repos."
> Behavior:
>
> * **All names present** — returns the matching `Repo` list in input
>   order.
> * **Any names missing** (including the all-missing case) — calls
>   `pytest.fail(...)` with a clear "misconfigured run" message.
>   Hard-coded closure tests are release-gating invariants that are
>   only meaningful with the full named set provided; silently
>   skipping such a check is worse than failing loudly. Use
>   `pytest -k` / `--ignore` to deselect a hard-coded test if you
>   intentionally don't want to run it.
>
> Use the looser `binary_repos` / `srpm_repos` / `debuginfo_repos`
> fixtures when partial coverage should be tolerated.

`repo_packages`, `all_binary_packages`, and `cross_repo_file_index`
are returned as *callables* (not direct values) so each test can
invoke them with the test-time `arch` (and `repo`) instead of having
the fixture know which arch to pre-compute. The underlying
`MetadataService` memoizes results, so calling them many times is
cheap.

## The service layer

### `MetadataService` (`utils/metadata.py`)

Wraps the raw `repodata.py` loader with caching keyed by `(repo
fingerprint, arch)`. Translates loader errors into `pytest.fail` so
tests don't see noisy tracebacks from below the abstraction. Provides
the high-level operations the fixtures need:

* `list_packages(repo, arch) -> list[Package]`
* `build_file_index(repos, arch) -> dict[path, list[FileOwner]]` —
  first-pass path-overlap candidates (skips dirs and ghosts).
* `fetch_package_files(repo, package, arch) -> dict[path, FileMeta]` —
  on-demand RPM download (via librepo) plus per-file metadata
  extraction (mode/owner/group/size/digest/linkto via python3-rpm).
  Memoized per NEVRA. The file-conflicts test calls this only for
  the small set of packages involved in candidate overlaps, then
  applies `rpmfilesCompare`-equivalent rules.
* `fetch(repo, arch) -> RepoLayout` — exposes the on-disk paths of
  the librepo-fetched repomd/primary/filelists. Used by `Repoclosure`
  so the metadata cache is shared (no double fetch).

### `Repoclosure` (`utils/repoclosure.py`)

In-process repoclosure runner. Builds a `libdnf5.base.Base`, loads
each universe repo from the librepo-fetched cache as a `file://`
mirror, and for each checked package walks every `Requires` entry
through `PackageQuery.is_dep_satisfied` (libsolv's native rich-dep
evaluator). Reports each requirement that has no provider in the
universe.

```python
def run(
    self,
    *,
    target_repos: list[Repo],
    arch: str,
    universe_repos: list[Repo] | None = None,
    check_kind: str = "binary",  # "binary" | "buildtime" | "all"
) -> RepoclosureResult: ...
```

`target_repos` are the repos whose packages we expect to close;
`universe_repos` (when wider than `target_repos`) are the repos that
contribute providers. `check_kind` selects which package arches the
*checker* examines:

* `"binary"` — `[arch, noarch]` — pure runtime closure of binary
  packages.
* `"buildtime"` — `[arch, noarch, src, nosrc]` — used by the SRPM
  build-time closure test. Catches BOTH unresolved BuildRequires
  (because src/nosrc are checked) AND runtime breakage in the binary
  packages that provide those BuildRequires (because arch/noarch are
  also checked). For `"buildtime"` we also disable the
  per-target-repo filter on findings: a binary provider from
  `base ∪ sdk` whose own runtime deps are broken is exactly the kind
  of cross-repo failure this kind exists to catch, so it must be
  surfaced even though its source repo is not in `target_repos`.
* `"all"` — no arch filter on the checker.

`libdnf5` is the libsolv binding `dnf5 repoclosure` itself uses, so
the rich-dep semantics match exactly: every `Requires` must have a
provider in the universe filtered to latest EVR per name (the
`best=1` model dnf uses at install time). Rich/boolean dependencies
(`if`, `unless`, `with`, `or`, `and`, `else`) are evaluated
correctly without any special handling in our code —
`pool_satisfieddep_map` treats them as boolean conditionals over
the available providers, so a `(foo if bar)` whose trigger has no
provider is correctly reported as satisfied.

We deliberately deviate from `dnf5 repoclosure` in **one** place:
the *to-check* set is also filtered to latest EVR per name. Stock
`dnf5 repoclosure` walks every NEVRA in the target repo, which
means a snapshot that publishes both N-1 and N of a tightly-pinned
package family (e.g. all of `azurelinux-release-*` carried at both
`-12.azl4` and `-13.azl4` mid-rebuild) reports the older `-12.azl4`
set as broken — its peers were filtered out by the latest-EVR
filter on the available side. That signal is technically true ("you
can no longer downgrade to `-12.azl4`") but not actionable: the
repo's *latest installable* state is the only thing closure is
meant to validate. Filtering the to-check side too means we
effectively ask *"would `dnf install <pkg>` actually pick a
closeable set?"* This still catches kernel/anaconda-style
version-pinning bugs (those have a *single* EVR pinning a
*missing* peer, not an *older* EVR pinning an older but present
peer), so no real signal is lost.

There is no subprocess shell-out; no JSON-vs-text output
schema-drift handling; no `--json` capability probe; no
host-vs-container backend split. The previous abstraction existed
only because older host `dnf5` builds lacked `--json`, which is
irrelevant when we drive the solver in-process.

## Implementation layer

### `utils/repodata.py`

A thin wrapper over `librepo` (fetch + verify + decompress) and
`createrepo_c` (parse). librepo writes `repomd.xml` and the
`primary` + `filelists` records into a per-repo cache directory,
verifying SHA checksums against repomd and decompressing zchunk /
zstd / xz / gz transparently. `createrepo_c.xml_parse_primary` and
`xml_parse_filelists` then stream the underlying file via libxml2 —
memory stays bounded for very large filelists (tens to hundreds of
MB uncompressed).

The previous implementation hand-rolled HTTP fetch with retries,
checksum verification, multi-format decompression (gz/xz/zstd via
`zstandard`), and `defusedxml.iterparse` of primary + filelists —
~700 lines that did exactly what librepo+createrepo_c already do.
The dnf stack uses these same libraries internally, so the two
codepaths are now guaranteed to interpret repodata identically.

### `utils/repoclosure.py`

A thin wrapper over `libdnf5`. `Repoclosure.run` builds a
`libdnf5.base.Base`, loads each universe repo from the
librepo-fetched cache (reusing the `MetadataService` cache as a
`file://` mirror), filters BOTH the available-providers query and
the to-check query to latest EVR per name (see semantics
discussion above), and walks every checked package's `Requires`
looking for entries that `PackageQuery.is_dep_satisfied` reports
as unsatisfied. `rpmlib(...)` and `solvable:prereqmarker` synthetic
deps are already filtered by libdnf5 (matching `dnf5 repoclosure`'s
own behaviour).

The previous implementation shelled out to `dnf5 repoclosure` and
parsed its output (JSON when available, falling back to a
line-oriented text parser); it shipped two backends (host and
container) plus an output-format-capability probe and per-finding
NEVRA reparser. All of that is gone — we just call libsolv via
libdnf5, in-process, with a few dozen lines.

An earlier in-process revision used `hawkey.Query.filter(provides=)`
on each Requires entry. That looked correct but actually treated
rich expressions as literal Provides strings (libsolv was being
asked "does any Solvable literally Provides the string
`(foo if bar)`?", which is never true), so every rich dep was
reported as unresolved. `libdnf5.rpm.PackageQuery.is_dep_satisfied`
is the call `dnf5 repoclosure` itself uses; it routes through
`pool_satisfieddep_map`, which evaluates the full rich grammar.

## Caching strategy

The session workdir defaults to a fresh `tempfile.mkdtemp(...)` and
is removed at session end. With `--workdir` set, it is reused as-is
and never cleaned (post-mortem friendly).

`MetadataService` writes repomd, primary, and filelists artifacts
under
`<workdir>/repodata/rv-<releasever-or-none>/<xdist-worker>/<arch>/<reponame>-<fingerprint>/`
where `fingerprint` is a stable short hash over `(name, kind, url)`.
The xdist-worker scope keeps two parallel pytest workers from racing
each other on the same destdir (librepo writes the same filenames
each run, so without per-worker scope two simultaneous fetches of
the same repo would clobber each other's `repomd.xml`).

This means **a reused workdir cannot serve stale metadata** if any of
url, arch, or releasever changes between runs. The repoclosure
runner reuses the same on-disk metadata, so there is never a double
fetch.

## URL placeholders

`librepo` substitutes `$basearch`, `$arch`, and `$releasever` directly
when fetching. We pass the chosen arch and releasever to librepo via
`Handle.varsub`; the URL stored in the `Repo` dataclass keeps the
placeholders verbatim so the same `Repo` object can be reused across
arches without mutation.

If a URL contains `$releasever` and `--releasever` was not provided,
the plugin raises `pytest.UsageError` at `pytest_configure` time so
the user gets a single clean message before any test starts.

## Why these libraries (and not stdlib + dnf5 shell-out)?

The previous design parsed repodata directly with stdlib XML to
avoid pulling in dnf — but that re-implemented exactly what
`createrepo_c` and `librepo` already do, including some sharp edges
(zchunk handling, atomic write semantics for parallel xdist runs,
zstd decompression on Python <3.14). Adopting the canonical
libraries:

* **Eliminates the host-vs-container backend split.** The split
  existed only because older host `dnf5` builds lacked
  `repoclosure --json`. Driving libsolv in-process via libdnf5 makes
  output parsing irrelevant — there is no output.
* **Removes ~1300 lines of infra code** (XML iterparse, HTTP retry
  loop, checksum verify, decompression fallbacks, JSON-vs-text
  parsers, NEVRA regex, JSON capability probe, `.repo` file
  rendering, subprocess plumbing, container bind-mount logic).
* **Aligns metadata interpretation with dnf** — when dnf changes
  its parsing of a quirky tag, we change with it for free.
* **Speeds up runs** — no subprocess fork per repoclosure
  invocation; metadata is parsed once per session and reused by
  every dependent test.

The new requirements are system packages (`python3-librepo`,
`python3-libdnf5`, `python3-rpm`), not pip-installable wheels.
This is consistent with the previous host-backend requirement on
the `dnf5` binary; users running the suite in a Fedora/AZL/RHEL
container or on those distros already have them. See
[`../README.md`](../README.md) for installation guidance.

## Adding a new test

See [`tests.md`](tests.md) — the section "How to add a new test"
walks through marker selection, fixture choice, and the
aggregate-vs-data-parametrize decision.
