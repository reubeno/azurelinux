# Azure Linux RPM repo validation tests

A pytest-based test suite that validates *published* RPM repositories
of the Azure Linux distribution. Tests are parametric over `(repo,
arch)` pairs and can be selectively scoped to specific repo *kinds*
(binary / srpm / debuginfo) or specific repo *names* (e.g., `base`,
`sdk`).

For the design rationale and the layered architecture (test
fixtures ↔ service layer ↔ implementation), see
[`docs/architecture.md`](docs/architecture.md). For the catalogue of
existing tests and how to add new ones, see
[`docs/tests.md`](docs/tests.md).

## Quick start

There are two ways to point the suite at a published repo set. Pick
whichever fits your workflow:

**A. One-shot, by URL prefix (the convenient form)** — assumes the
target uses the *Standard Azure Linux Repo Layout*:

```bash
cd base/packages/tests
.venv/bin/pytest cases/ \
    --repo-prefix https://<published-repo-root>/ \
    --arch x86_64 --arch aarch64
```

The prefix is expanded into the six conventional sub-repos
(`base`, `base-debuginfo`, `base-srpms`, `sdk`, `sdk-debuginfo`,
`sdk-srpms`) and any that 404 are silently dropped — so a partial
mirror works fine. This matches the layout produced by
[`scripts/synthesize-repodata.py`](../../../scripts/synthesize-repodata.py).

**B. Spell out individual repos (full control)** — useful for ad-hoc
URLs, mixed sources, or non-conventional naming:

```bash
cd base/packages/tests
.venv/bin/pytest cases/ \
    --repo 'name=base,kind=binary,url=https://<published-repo-base>/$basearch/' \
    --repo 'name=sdk,kind=binary,url=https://<published-repo-sdk>/$basearch/' \
    --repo 'name=base-srpms,kind=srpm,url=https://<published-repo-base-srpms>/' \
    --arch x86_64 --arch aarch64
```

The two forms can be combined freely; an explicit `--repo` overrides
a same-named entry that came from `--repo-prefix` (handy for pinning
one channel to a development URL while keeping the rest from the
published prefix).

Expected outcomes:

* Tests that don't apply to the provided repos (e.g., `vendor_tag` with
  only an SRPM repo provided) are reported as **skipped** with a
  message that names the missing kind/name. Skips are intentional —
  they do not fail the run.
* Tests fan out across `(repo, arch)` pairs; failures are reported
  per pair with ids like `base-x86_64`.

## Prerequisites

The test suite runs entirely in-process — there is no shell-out to
`dnf5` and no container backend. All work is done by the dnf-stack
Python libraries (`createrepo_c`, `librepo`, `libdnf5`).

| Dependency | Provided by | Notes |
| --- | --- | --- |
| Python 3.12+ | the host | |
| `createrepo_c` Python module | `pyproject.toml` (pip) | manylinux wheel on PyPI; pulled in automatically. |
| `python3-librepo`, `python3-libdnf5` | system package manager | NOT on PyPI. Install via your distro (`dnf install python3-librepo python3-libdnf5` on Fedora/AZL/RHEL; `apt install python3-librepo python3-libdnf5` on Debian/Ubuntu). |
| Network access to the repo URLs | the host | |

`librepo` handles the metadata fetch (with checksum verification,
zchunk/zstd/xz/gz decompression, and atomic-rename caching);
`createrepo_c` parses primary/filelists; `libdnf5` (libsolv) drives
repoclosure — the same call (`PackageQuery.is_dep_satisfied`) that
`dnf5 repoclosure` itself uses, so rich/boolean dependencies (`if`,
`unless`, `with`, `or`, `and`, `else`) are evaluated correctly. These
are the same libraries `dnf` itself uses internally — so our metadata
interpretation is guaranteed to match dnf's.

> ⚠️ **`uv run pytest` does not work out of the box.** `uv` creates
> isolated venvs and currently has no `--system-site-packages`
> equivalent, so the system-only `python3-librepo` / `python3-libdnf5`
> are invisible from inside a `uv`-managed venv. Use the stdlib
> `venv` workflow shown under [Invocation](#invocation) instead.
> (`pytest --collect-only` and `pytest --help` still work under `uv`
> because the dnf-stack libraries are lazy-imported, so collection
> errors are not silent — but actually fetching a repo will fail
> with a clear "install python3-librepo via your system package
> manager" message.)

## Invocation

The canonical setup is a stdlib venv that exposes the system
dnf-stack modules:

```bash
# one-time setup
python -m venv --system-site-packages .venv
.venv/bin/pip install -e base/packages/tests

# every run
.venv/bin/pytest base/packages/tests/cases/ ...
```

`--system-site-packages` is required so the venv can see
`python3-librepo` / `python3-libdnf5` from the
system. The project's `pip install -e .` registers the `pytest11`
plugin entry point that wires up the suite's CLI options.

If you prefer not to maintain a venv, `pip install --user -e
base/packages/tests` followed by bare `pytest base/packages/tests/cases/
...` also works on most distros (PEP-668 permitting).

### CLI options

| Option | Repeatable | Default | Description |
| --- | --- | --- | --- |
| `--repo` | yes | — (none required, but most tests skip without it) | Add a repo. Format: `name=...,kind=...,url=...` (comma-separated `key=value`). `kind` ∈ `binary` / `srpm` / `debuginfo`. URL may contain `$basearch` / `$arch` / `$releasever` placeholders — these are substituted by `librepo` at fetch time. Repo names must be globally unique across all `--repo` and `--repos-file` inputs. |
| `--repos-file` | yes | — | Load repos from a yum/dnf-style `.repo` ini file. Each section is one repo (name = section header, `baseurl=` for URL, plus a custom `kind=` key). Combine freely with `--repo`. |
| `--repo-prefix` | yes | — | Convenience shorthand: assume the URL hosts the *Standard Azure Linux Repo Layout* and expand it into the six conventional sub-repos (`base`, `base-debuginfo`, `base-srpms`, `sdk`, `sdk-debuginfo`, `sdk-srpms`). Each is probed for `repodata/repomd.xml`; sub-repos that 404 are silently skipped (so partial mirrors work). All-404 / connection errors are fatal. Binary/debuginfo URLs are probed against the first `--arch` as a sentinel and registered with a `$basearch` placeholder, so they still fan out across every `--arch` at fetch time. Explicit `--repo` / `--repos-file` definitions override same-named entries from a prefix. See [Examples → Use `--repo-prefix`](#use---repo-prefix). |
| `--arch` | yes | `x86_64` | Architecture to test against. Substituted for `$basearch` / `$arch` in repo URLs. The first `--arch` (after dedup) is also used as the probing arch for `--repo-prefix`. |
| `--releasever` | no | unset | Required iff at least one URL contains `$releasever`. Never inherited from the host. |
| `--workdir` | no | fresh `tempfile.mkdtemp(prefix="azl-repo-tests-")` | If set, used as-is and not cleaned (post-mortem friendly). |
| `--expected-vendor` | no | `Microsoft Corporation` | Vendor string every binary package must declare (checked by `test_vendor_tag`). |
| `--release-suffix` | no | `\.azl4(~.*)?$` | Regex (`re.search`) every binary package's Release tag must match (checked by `test_release_suffix`). Override for AZL3 (e.g., `\.azl3(~.*)?$`) or other distros. |

### Selecting tests

Standard pytest selection works. To run only a few tests:

```bash
.venv/bin/pytest cases/test_vendor_tag.py --repo ...
.venv/bin/pytest -k 'repoclosure'          --repo ...
.venv/bin/pytest cases/test_blocklist.py   --repo ...
```

To run only against the `base` repo:

```bash
.venv/bin/pytest cases/ --repo name=base,kind=binary,url=...
```

* Tests scoped to other repo kinds/names will skip with a clear
  "no --repo matched markers ..." message.
* Tests hard-coded for a specific repo set (e.g.
  `test_repoclosure_base_plus_sdk`, `test_repoclosure_base_srpms_buildtime`)
  fail loudly if any of their named repos are missing — they are
  release-gating invariants that are only meaningful with the full
  set provided. Use `pytest -k` / `--ignore` to deselect them
  intentionally.
* Cross-repo tests that need at least one binary repo
  (`test_no_duplicate_subpackage_names`, `test_file_conflicts_*`)
  also fail when no binary `--repo` is provided, for the same
  reason.

## Examples

### Validate just an SRPM repo

```bash
.venv/bin/pytest cases/ \
    --repo 'name=base-srpms,kind=srpm,url=https://example.com/srpms/'
```

This runs the `test_only_srpms_in_srpm_repo` test on `base-srpms` and
skips every binary-only / debuginfo-only test.

### Cross-arch validation in one invocation

```bash
.venv/bin/pytest cases/ \
    --repo 'name=base,kind=binary,url=https://example.com/base/$basearch/' \
    --arch x86_64 --arch aarch64
```

Each test that depends on `(repo, arch)` runs once per arch. Use
`-n auto` (with `pytest-xdist`, if installed) to parallelize.

### Use a `.repo` ini file

```bash
cat > azl.repo <<EOF
[base]
baseurl=https://example.com/base/\$basearch/
kind=binary

[base-srpms]
baseurl=https://example.com/srpms/
kind=srpm
EOF

.venv/bin/pytest cases/ --repos-file azl.repo --arch x86_64
```

The same flag may be repeated to load several files; freely combinable
with inline `--repo` flags.

### Use `--repo-prefix`

When the target uses the *Standard Azure Linux Repo Layout* (the same
layout produced by `scripts/synthesize-repodata.py`), `--repo-prefix`
replaces six `--repo` flags with one URL:

```bash
.venv/bin/pytest cases/ \
    --repo-prefix https://example.com/published/ \
    --arch x86_64 --arch aarch64
```

This probes each of the six conventional sub-repos and registers the
ones that exist:

| Probed URL                                          | Registered as          | Kind        |
| --------------------------------------------------- | ---------------------- | ----------- |
| `<prefix>/base/<arch>/repodata/repomd.xml`          | `base`                 | `binary`    |
| `<prefix>/base/debuginfo/<arch>/repodata/repomd.xml`| `base-debuginfo`       | `debuginfo` |
| `<prefix>/base/srpms/repodata/repomd.xml`           | `base-srpms`           | `srpm`      |
| `<prefix>/sdk/<arch>/repodata/repomd.xml`           | `sdk`                  | `binary`    |
| `<prefix>/sdk/debuginfo/<arch>/repodata/repomd.xml` | `sdk-debuginfo`        | `debuginfo` |
| `<prefix>/sdk/srpms/repodata/repomd.xml`            | `sdk-srpms`            | `srpm`      |

A sub-repo that 404s is silently dropped (so a partial mirror — say,
`base` published but `sdk` not yet — just works). Other HTTP / network
errors are fatal: a typo'd hostname won't masquerade as "no repos
present". If *all six* probes 404, the prefix itself is presumed
bogus and the run aborts.

The probe uses the first `--arch` (after dedup) as a sentinel; the
registered repo URL keeps the `$basearch` placeholder so it still fans
out across every `--arch` at fetch time. If your prefix only publishes
some arches, fall back to explicit `--repo` for those channels.

You can mix `--repo-prefix` with `--repo` / `--repos-file` to override
specific sub-repos:

```bash
.venv/bin/pytest cases/ \
    --repo-prefix https://example.com/published/ \
    --repo 'name=sdk,kind=binary,url=https://staging.example.com/dev-sdk/$basearch/'
```

Here `sdk` comes from the explicit URL; the other five come from the
prefix.

### Reuse a workdir for fast re-runs

```bash
.venv/bin/pytest cases/ \
    --repo 'name=base,kind=binary,url=https://example.com/base/$basearch/' \
    --workdir /tmp/azl-repo-tests
```

The first run downloads repomd / primary / filelists into the
workdir; subsequent runs reuse them. Cache subdirs are keyed by a
fingerprint of `(repo, arch, releasever)` so changing any of them
will not serve stale metadata.

## Layout

```
base/packages/tests/
├── README.md                # this file
├── docs/
│   ├── architecture.md      # design + layers + rationale
│   └── tests.md             # test catalogue + how to add a new test
├── pyproject.toml
├── conftest.py              # fixtures (the test-facing surface)
├── cases/                   # individual test files
└── utils/                   # service + implementation modules
```
