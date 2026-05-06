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

```bash
cd base/packages/tests
uv run pytest cases/ \
    --repo 'name=base,kind=binary,url=https://<published-repo-base>/$basearch/' \
    --repo 'name=sdk,kind=binary,url=https://<published-repo-sdk>/$basearch/' \
    --repo 'name=base-srpms,kind=srpm,url=https://<published-repo-base-srpms>/' \
    --arch x86_64 --arch aarch64
```

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
Python libraries (`createrepo_c`, `librepo`, `hawkey`).

| Dependency | Provided by | Notes |
| --- | --- | --- |
| Python 3.12+ + `uv` (or `pip` + a virtualenv) | the host | |
| `createrepo_c` Python module | pip / `pyproject.toml` | manylinux wheels on PyPI; pulled in automatically. |
| `python3-librepo`, `python3-hawkey`, `python3-libdnf` | system package manager | NOT on PyPI. Install via your distro (`dnf install python3-librepo python3-hawkey` on Fedora/AZL/RHEL; `apt install python3-librepo python3-hawkey python3-libdnf` on Debian/Ubuntu). |
| Network access to the repo URLs | the host | |

`librepo` handles the metadata fetch (with checksum verification,
zchunk/zstd/xz/gz decompression, and atomic-rename caching);
`createrepo_c` parses primary/filelists; `hawkey` (libsolv) drives
repoclosure. These are the same libraries `dnf` itself uses
internally — so our metadata interpretation is guaranteed to match
dnf's.

## Invocation

The canonical invocation is `uv run pytest cases/ ...` from the
`base/packages/tests/` directory. `uv` is required for the `pytest11`
plugin entry point to register the project's CLI options before
pytest parses argv. (If you have already done a `pip install -e .`
into the active env, bare `pytest cases/ ...` works too.)

### CLI options

| Option | Repeatable | Default | Description |
| --- | --- | --- | --- |
| `--repo` | yes | — (none required, but most tests skip without it) | Add a repo. Format: `name=...,kind=...,url=...` (comma-separated `key=value`). `kind` ∈ `binary` / `srpm` / `debuginfo`. URL may contain `$basearch` / `$arch` / `$releasever` placeholders — these are substituted by `librepo` at fetch time. Repo names must be globally unique across all `--repo` and `--repos-file` inputs. |
| `--repos-file` | yes | — | Load repos from a yum/dnf-style `.repo` ini file. Each section is one repo (name = section header, `baseurl=` for URL, plus a custom `kind=` key). Combine freely with `--repo`. |
| `--arch` | yes | `x86_64` | Architecture to test against. Substituted for `$basearch` / `$arch` in repo URLs. |
| `--releasever` | no | unset | Required iff at least one URL contains `$releasever`. Never inherited from the host. |
| `--workdir` | no | fresh `tempfile.mkdtemp(prefix="azl-repo-tests-")` | If set, used as-is and not cleaned (post-mortem friendly). |
| `--expected-vendor` | no | `Microsoft Corporation` | Vendor string every binary package must declare (checked by `test_vendor_tag`). |
| `--release-suffix` | no | `\.azl4(~.*)?$` | Regex (`re.search`) every binary package's Release tag must match (checked by `test_release_suffix`). Override for AZL3 (e.g., `\.azl3(~.*)?$`) or other distros. |

### Selecting tests

Standard pytest selection works. To run only a few tests:

```bash
uv run pytest cases/test_vendor_tag.py --repo ...
uv run pytest -k 'repoclosure'          --repo ...
uv run pytest cases/test_blocklist.py   --repo ...
```

To run only against the `base` repo:

```bash
uv run pytest cases/ --repo name=base,kind=binary,url=...
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
uv run pytest cases/ \
    --repo 'name=base-srpms,kind=srpm,url=https://example.com/srpms/'
```

This runs the `test_only_srpms_in_srpm_repo` test on `base-srpms` and
skips every binary-only / debuginfo-only test.

### Cross-arch validation in one invocation

```bash
uv run pytest cases/ \
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

uv run pytest cases/ --repos-file azl.repo --arch x86_64
```

The same flag may be repeated to load several files; freely combinable
with inline `--repo` flags.

### Reuse a workdir for fast re-runs

```bash
uv run pytest cases/ \
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
