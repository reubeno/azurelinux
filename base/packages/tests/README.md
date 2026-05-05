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

| If you use... | You need on the host |
| --- | --- |
| `--repoclosure-backend host` (default) | `dnf5` (with the `dnf5-plugins` package, which provides `repoclosure`). |
| `--repoclosure-backend container` | `podman` or `docker`. The container image (default `fedora:44`) must have `dnf5` available. |
| Either | Network access to the repo URLs, plus Python 3.12+ and `uv` (or `pip` + a virtualenv). |

The metadata-only tests (everything except `test_repoclosure_*.py`)
fetch and parse repodata directly with the Python stdlib — no `dnf` is
involved for those, so they have no host runtime requirement beyond
network access.

## Invocation

The canonical invocation is `uv run pytest cases/ ...` from the
`base/packages/tests/` directory. `uv` is required for the `pytest11`
plugin entry point to register the project's CLI options before
pytest parses argv. (If you have already done a `pip install -e .`
into the active env, bare `pytest cases/ ...` works too.)

### CLI options

| Option | Repeatable | Default | Description |
| --- | --- | --- | --- |
| `--repo` | yes | — (none required, but most tests skip without it) | Add a repo. Format: `name=...,kind=...,url=...` (comma-separated `key=value`). `kind` ∈ `binary` / `srpm` / `debuginfo`. URL may contain `$basearch` / `$arch` / `$releasever` placeholders — these are pre-substituted by the suite (so cross-arch validation actually fetches the requested arch's metadata, even on a host of a different arch). Repo names must be globally unique across all `--repo` flags. |
| `--arch` | yes | `x86_64` | Architecture to test against. Substituted for `$basearch` / `$arch` in `--repo` URLs. |
| `--releasever` | no | unset | Required iff at least one URL contains `$releasever`. Never inherited from the host or container. |
| `--repoclosure-backend` | no | `host` | `host` shells out to local `dnf5`; `container` runs dnf5 inside `--container-image`. |
| `--container-image` | no | `fedora:44` | Image used by the container backend. |
| `--container-runtime` | no | auto | `podman` (preferred) or `docker`. |
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

### Use the container backend

```bash
uv run pytest cases/ \
    --repo 'name=base,kind=binary,url=https://example.com/base/$basearch/' \
    --repoclosure-backend container \
    --container-image fedora:44
```

Useful when the host's `dnf5` is too old to support the features the
tests rely on (e.g., `dnf5 repoclosure --json`).

### Use the container backend

The container backend runs `dnf5 repoclosure` inside a configurable
container image (default `fedora:44`) instead of relying on the
host's `dnf5`. Use it when:

* the host's `dnf5` is older than ~5.4 (no `repoclosure --json`,
  yielding less-structured output);
* you want hermetic, reproducible behavior across machines;
* you don't have `dnf5` on the host at all.

```bash
uv run pytest cases/ \
    --repo 'name=base,kind=binary,url=https://example.com/base/$basearch/' \
    --repoclosure-backend container \
    --container-image fedora:44
```

Prerequisites:

* `podman` (preferred) or `docker` on `PATH` (autodetect; override
  with `--container-runtime podman` / `--container-runtime docker`).
* Ability to pull the image. The first run pulls `fedora:44`
  (~200 MB); subsequent runs reuse the cached image.
* Network access from inside the container (default container
  network is fine; `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` /
  `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` host env vars are forwarded
  into the container automatically).

What it does behind the scenes:

* Renders a `.repo` file under `<workdir>/container-backend/...`
  (separate cache subtree from the host backend so the two can
  coexist without contamination).
* Bind-mounts that subtree into the container at `/azl-repo-tests`
  with `:Z` only when SELinux is detected on the host
  (`/sys/fs/selinux` exists).
* Runs `dnf5 repoclosure` once per (target repo set, arch) inside
  the container. Probes `--json` support once per session and uses
  it when available — the JSON parser captures per-package source
  repo, which the text-output fallback cannot.
* Each invocation is a separate `podman run --rm`, so there's a
  small overhead (~200ms) per call. With `--workdir <dir>` set,
  metadata caches persist across calls, keeping the second and
  later invocations cheap.

If you see `dnf5` not found errors inside the container, you're on
an older Fedora image without `dnf5` preinstalled; use a newer
image (e.g. `fedora:44` or later) or pass an image that has the
`dnf5` and `dnf5-plugins` packages.

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
