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
   utils/           │  MetadataService           RepoBackend      │
   (service)        │  (cache + iterate)         (repoclosure)    │
                    └────────────┬───────────────────┬────────────┘
                                 │                   │
                    ┌────────────▼─────────┐  ┌──────▼─────────────┐
   utils/           │  repodata.py         │  │  backends/host.py  │
   (implementation) │  (fetch+streamparse) │  │  backends/container│
                    └──────────────────────┘  └────────────────────┘
```

**Tests never reach below the fixture layer.** If you find yourself
wanting to import `utils.repodata` or `utils.backends.*` from a test
file, that's a signal to extend `MetadataService` (or add a fixture)
instead.

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

### `--repo` syntax

```
--repo name=...,kind=...,url=...
```

The format is strictly `key=value` segments separated by commas. The
*first* `=` in each segment separates key from value, so values
containing `=` are tolerated. Values containing commas are not
supported — if a real-world URL ever needs commas, swap to a
URL-encoded form. URL placeholders like `$basearch` and `$releasever`
are passed through verbatim to dnf; the metadata-only loader does its
own substitution (see [`utils/repodata.py:substitute_url`](../utils/repodata.py)).

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
| `cross_repo_file_index(arch)` | function | `dict[path, list[FileOwner]]` | the file-conflicts test |
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
* `build_file_index(repos, arch) -> dict[path, list[FileOwner]]`

### `RepoBackend` (`utils/backends/base.py`)

Narrow interface focused on the only operation that genuinely needs
dnf:

```python
def repoclosure(
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
  also checked). A binary provider that itself doesn't close is not a
  usable build input, so the broader check reflects what "buildable"
  actually requires.
* `"all"` — no arch filter on the checker.

Two concrete implementations:

| Backend | What it does | Cache subdir |
| --- | --- | --- |
| `HostBackend` (`utils/backends/host.py`) | Shells out to the local `dnf5`. Renders a self-contained `.repo` file in `<workdir>/host-backend/...`, with `--setopt=reposdir=...` so the host's system dnf state is *not* touched. | `<workdir>/host-backend/...` |
| `ContainerBackend` (`utils/backends/container.py`) | Runs the same dnf5 invocations inside a configurable container (default `fedora:44`). Bind-mounts a *separate* subdir of the workdir; uses `:Z` only when SELinux is detected; forwards proxy/CA env vars. | `<workdir>/container-backend/...` |

Both probe `dnf5 repoclosure --help` once per session to detect
`--json` support. If present, the JSON parser produces structured
`RepoclosureResult.unresolved` entries; otherwise we fall back to a
text-output parser (see `utils/dnf.py`).

## Implementation layer

### `utils/repodata.py`

Fetches `repodata/repomd.xml`, picks the best available `primary` and
`filelists` records (skipping `.zck` because zstd is not in the Python
stdlib), and stream-parses them with `xml.etree.ElementTree.iterparse`.
Stream parsing matters for `filelists.xml`, which in real repos can
exceed 100 MB uncompressed.

Verifies SHA checksums when repomd advertises them. Mirrors the
relative href under the cache directory so two records with the same
basename in different subdirs don't collide.

### `utils/dnf.py`

Pure helpers shared by both backends:

* `render_repo_file(repos, *, arch, releasever)` — emit a `.repo`
  file body. **Pre-substitutes** `$basearch` / `$arch` / `$releasever`
  in each `baseurl` so cross-arch validation actually fetches the
  requested arch's metadata. (Without this, dnf would substitute
  `$basearch` from the running host's arch, then `--arch=<other>`
  would filter checks to zero packages — silently green.)
* `build_repoclosure_argv(...)` — construct the argv consistently.
  Always passes `--arch=<arch> --arch=noarch` for repoclosure (per
  dnf5 docs; this is the documented surface for filtering *which
  packages get checked*. URL substitution is handled by
  `render_repo_file` above, not by `--forcearch`).
* `probe_repoclosure_json(run)` — detect `--json` support.
* `parse_json_repoclosure_output(...)` /
  `parse_text_repoclosure_output(...)` — turn dnf5 output into a
  `RepoclosureResult`.

## Caching strategy

The session workdir defaults to a fresh `tempfile.mkdtemp(...)` and
is removed at session end. With `--workdir` set, it is reused as-is
and never cleaned (post-mortem friendly).

All caches are keyed by a stable fingerprint that includes
`(name, kind, url)` of the repo plus `arch` and (where applicable)
`releasever`:

* `MetadataService` writes repomd, primary, and filelists artifacts
  under `<workdir>/repodata/rv-<releasever-or-none>/<arch>/<reponame>-<fingerprint>/`.
* `HostBackend` writes its rendered `.repo` file and dnf cache under
  `<workdir>/host-backend/rv-<releasever-or-none>/<arch>/<targets-slug>/`.
* `ContainerBackend` writes its bind-mount subdir under
  `<workdir>/container-backend/...` so it never collides with the
  host backend's caches.

This means **a reused workdir cannot serve stale metadata** if any of
url, arch, or releasever changes between runs.

## URL placeholders

Two layers handle them:

* For *direct fetches* (the metadata-only path), `utils.repodata.substitute_url`
  substitutes `$basearch`, `$arch`, `$releasever` with the chosen
  values before issuing the HTTP request. If a URL contains
  `$releasever` and `--releasever` was not provided, this raises an
  error — but the plugin also performs the same check at
  `pytest_configure` time, so the user gets a single clean message
  before any test starts.
* For *dnf-driven invocations* (repoclosure), the URL is left
  untouched in the rendered `.repo` file. dnf substitutes `$basearch`
  from the per-command `--arch=<arch>` and `$releasever` from
  `--setopt=releasever=<value>`. We never let `$releasever` inherit
  from the host or container — both the host backend and the
  container backend pass `--setopt=releasever=...` explicitly when a
  releasever is configured.

## Why parse repodata directly?

For metadata-only checks (vendor, blocklist, file conflicts, ...),
parsing repodata is faster and simpler than driving `dnf`:

* No external process invocation per query.
* No surface area for cross-version `dnf5` quirks (especially around
  SRPM querying, which has had several bug fixes in recent dnf5
  versions).
* Native streaming: we never load a large `filelists.xml` into memory.

`dnf5` is reserved for `repoclosure` — the one operation that
genuinely needs a full SAT solver to reason about runtime
dependencies.

## Adding a new test

See [`tests.md`](tests.md) — the section "How to add a new test"
walks through marker selection, fixture choice, and the
aggregate-vs-data-parametrize decision.
