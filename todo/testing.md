# Plan: Static Image Validation with Pytest

## TL;DR

Build a pytest-based framework in `base/images/` to statically validate built Azure Linux images (VM and container) without booting them. Uses `guestmount` (libguestfs FUSE) for VM images and `podman image mount` for OCI containers to mount filesystems read-only, then provides layered pytest fixtures for test authors. One image per invocation, managed with `uv`.

## Architecture

### Mounting Strategy

**VM images (raw/VHD):**
- `guestmount --ro -a <image> -i <mountpoint>` — full read-only FUSE mount (uses `LIBGUESTFS_BACKEND=direct` to avoid libvirt/SELinux issues)
- `virt-inspector -a <image>` — disk-level metadata (partitions, fs types, labels)
- Cleanup: `guestunmount <mountpoint>` in fixture teardown

**Container images (OCI tar.xz):**
- `skopeo copy oci-archive:<image> oci:<layout>:latest` — convert OCI archive to OCI layout
- `umoci unpack --rootless --image <layout>:latest <bundle>` — extract rootfs without root privileges
- Cleanup: `buildah unshare rm -rf <extract_dir>` (handles read-only dirs from rootless extraction)

Both paths expose a directory with full filesystem fidelity — real permissions, ownership, symlinks. Tests access any path on-demand (lazy I/O). Uses CLI tools (not Python bindings) to avoid system site-packages issues with uv.

### CLI Interface

```
cd base/images && uv run pytest --image-name vm-base --image-path /path/to/image.vhd
```

- `--image-name` (required): Image name matching key in `images.toml` (e.g., `vm-base`, `container-base`)
- `--image-path` (required): Path to the built image artifact (VHD file, OCI tar.xz, etc.)
- One image per invocation (keeps fixtures simple, avoids cross-image state)

### Fixture Layers

All session-scoped (extraction is expensive — do it once, share across all tests).

**Core fixtures (conftest.py):**
| Fixture | Type | Description |
|---------|------|-------------|
| `image_name` | `str` | From `--image-name` CLI option |
| `image_path` | `Path` | From `--image-path` CLI option |
| `image_type` | `str` | `"vm"` or `"container"`, detected from azldev config / KIWI definition type |
| `azldev_config` | `dict` | Resolved TOML config from `azldev config dump -q -O json` |
| `rootfs` | `Path` | Path to extracted filesystem directory |
| `disk_info` | `DiskInfo \| None` | Partition table, filesystem info (VM only; `None` for containers) |

**Rich parsed fixtures (conftest.py, built on core fixtures):**
| Fixture | Type | Description |
|---------|------|-------------|
| `os_release` | `dict[str, str]` | Parsed `/etc/os-release` key-value pairs |
| `installed_packages` | `set[str]` | Set of installed RPM package names (NVRAs) |
| `yum_repos` | `list[RepoInfo]` | Parsed `/etc/yum.repos.d/*.repo` files |
| `enabled_services` | `set[str]` | Systemd services enabled via symlinks in `*.wants/` |
| `partition_table` | `list[PartitionInfo]` | Partition metadata — **auto-skips** for container images |
| `kernel_cmdline` | `str` | Default kernel command line from GRUB config — **auto-skips** for containers |
| `file_stat` | `Callable[[str], StatResult]` | Helper callable: `file_stat("/etc/shadow")` returns permissions/ownership |

Auto-skip pattern: fixtures that only apply to one image type call `pytest.skip("not applicable to {image_type} images")` when invoked for the wrong type. Tests that request them are automatically skipped without explicit markers.

### Test Organization & Collection

**Directory layout:**
```
base/images/
├── pyproject.toml              # uv project: pytest, dependencies
├── conftest.py                 # Root: CLI opts, pytest hooks, ALL fixtures
├── image_test/                 # Helper package (parsing, extraction logic)
│   ├── __init__.py
│   ├── extract.py              # VM extraction (virt-copy-out), container extraction (skopeo+umoci)
│   ├── disk.py                 # virt-filesystems / virt-inspector parsing
│   ├── parsers.py              # os-release, .repo file, systemd unit parsers
│   └── types.py                # Dataclasses: DiskInfo, PartitionInfo, RepoInfo, StatResult
├── tests/                      # Shared tests (run for ALL images)
│   ├── test_os_release.py      # Validate /etc/os-release fields
│   ├── test_repos.py           # Validate yum repo configuration
│   ├── test_packages.py        # Validate expected/unexpected packages
│   ├── test_services.py        # Validate enabled/disabled services
│   └── test_filesystem.py      # Validate file permissions, ownership
├── vm-base/
│   ├── vm-base.kiwi            # (existing)
│   └── tests/
│       ├── test_partitions.py  # Validate partition layout, labels, sizes
│       ├── test_bootloader.py  # Validate GRUB config, UEFI setup
│       └── test_kernel.py      # Validate kernel cmdline, modules
├── container-base/
│   ├── container-base.kiwi     # (existing)
│   └── tests/
│       └── test_container.py   # Validate container metadata, entrypoint, minimal footprint
└── images.toml                 # (existing)
```

**Collection filtering:** `conftest.py` implements `pytest_ignore_collect` hook. When `--image-name vm-base` is given:
- `tests/` → always collected (shared tests)
- `vm-base/tests/` → collected (matches --image-name)
- `container-base/tests/` → ignored (doesn't match)
- `image_test/` → never collected (not a test dir)

### Helper Package: `image_test/`

**`extract.py`** — Image mounting orchestration:
- `mount_vm_image(image_path: Path, mountpoint: Path) -> Path` — runs `guestmount --ro -a <image> -i <mountpoint>`, returns mountpoint
- `unmount_vm_image(mountpoint: Path) -> None` — runs `guestunmount <mountpoint>`
- `mount_container_image(image_path: Path) -> tuple[str, Path]` — runs `podman load` + `podman image mount`, returns (image_id, mountpoint)
- `unmount_container_image(image_id: str) -> None` — runs `podman image umount`
- `detect_image_type(image_name: str, azldev_config: dict) -> str` — looks up image in config, reads KIWI definition type attribute, returns `"vm"` or `"container"`

**`disk.py`** — VM disk inspection:
- `inspect_disk(image_path: Path) -> DiskInfo` — runs `virt-filesystems` and `virt-inspector`, parses output
- Returns: partitions, filesystem types, sizes, labels, mount points

**`parsers.py`** — File content parsers:
- `parse_os_release(content: str) -> dict[str, str]` — parses KEY=VALUE format
- `parse_repo_file(content: str) -> list[RepoInfo]` — parses INI-style .repo files
- `parse_systemd_enabled(etc_systemd_dir: Path) -> set[str]` — walks `*.wants/` symlinks
- `parse_grub_defaults(content: str) -> dict[str, str]` — parses `/etc/default/grub`
- `query_rpm_packages(rootfs: Path) -> set[str]` — runs `rpm --root <rootfs> -qa`

**`types.py`** — Data classes:
- `DiskInfo`, `PartitionInfo`, `FilesystemInfo`, `RepoInfo`, `StatResult`

### pyproject.toml

```toml
[project]
name = "azl-image-tests"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "pytest>=8.0",
]

[tool.pytest.ini_options]
addopts = ["-v", "--tb=short"]
markers = [
    "vm_only: test only runs for VM images",
    "container_only: test only runs for container images",
]
```

System-level dependencies (documented in README, not pip-installable):
- `libguestfs-tools` — provides `guestmount`, `guestunmount`, `virt-inspector`
- `podman` — rootless container image mounting
- `rpm` — for `rpm --root` package queries

## Steps

### Phase 1: Project Scaffolding
1. Create `base/images/pyproject.toml` with uv project config and pytest settings
2. Create `base/images/image_test/` package with `__init__.py` and `types.py` (dataclasses)
3. Create `base/images/conftest.py` with CLI option registration (`pytest_addoption`), `pytest_ignore_collect` hook, and core fixtures (`image_name`, `image_path`)

### Phase 2: Extraction Infrastructure
4. Implement `image_test/extract.py` — VM mounting via `guestmount`, container mounting via `podman image mount`, image type detection (*depends on 3*)
5. Implement `image_test/disk.py` — `virt-filesystems` / `virt-inspector` output parsing (*parallel with 4*)
6. Wire mounting into `conftest.py` session yield-fixtures: `rootfs`, `image_type`, `disk_info` — yield fixtures handle cleanup (guestunmount / podman image umount) on teardown (*depends on 4, 5*)

### Phase 3: Parsers & Rich Fixtures
7. Implement `image_test/parsers.py` — all file parsers (os-release, repo, systemd, grub, rpm query) (*parallel with 6*)
8. Implement `azldev_config` fixture in `conftest.py` — runs `azldev config dump -q -O json`, caches result (*parallel with 7*)
9. Wire rich fixtures in `conftest.py`: `os_release`, `installed_packages`, `yum_repos`, `enabled_services`, `partition_table`, `kernel_cmdline`, `file_stat` (*depends on 6, 7, 8*)

### Phase 4: Shared Tests
10. `tests/test_os_release.py` — validate NAME, VERSION_ID, ID, etc. (*depends on 9*)
11. `tests/test_repos.py` — validate repo files exist, required repos present (*parallel with 10*)
12. `tests/test_packages.py` — validate key packages installed, no blacklisted packages (*parallel with 10*)
13. `tests/test_services.py` — validate expected services enabled (*parallel with 10*)
14. `tests/test_filesystem.py` — validate file permissions, ownership on security-critical files (*parallel with 10*)

### Phase 5: Per-Image Tests
15. `vm-base/tests/test_partitions.py` — validate partition layout, EFI partition, filesystem types (*depends on 9*)
16. `vm-base/tests/test_bootloader.py` — validate GRUB config, UEFI boot (*parallel with 15*)
17. `vm-base/tests/test_kernel.py` — validate kernel cmdline defaults (console=ttyS0, etc.) (*parallel with 15*)
18. `container-base/tests/test_container.py` — validate minimal package set, no unnecessary services (*parallel with 15*)

### Phase 6: Documentation & Polish
19. Add a brief README.md in `base/images/` documenting how to run tests, system dependencies, adding new tests (*depends on all above*)

## Relevant Files

### New files to create
- `base/images/pyproject.toml` — uv project config
- `base/images/conftest.py` — root conftest with all fixtures and hooks
- `base/images/image_test/__init__.py` — package init
- `base/images/image_test/types.py` — dataclasses for DiskInfo, RepoInfo, etc.
- `base/images/image_test/extract.py` — image extraction logic (virt-copy-out, skopeo+umoci)
- `base/images/image_test/disk.py` — disk inspection (virt-filesystems/virt-inspector)
- `base/images/image_test/parsers.py` — file content parsers
- `base/images/tests/test_os_release.py` — shared os-release tests
- `base/images/tests/test_repos.py` — shared repo tests
- `base/images/tests/test_packages.py` — shared package tests
- `base/images/tests/test_services.py` — shared service tests
- `base/images/tests/test_filesystem.py` — shared filesystem tests
- `base/images/vm-base/tests/test_partitions.py` — VM partition tests
- `base/images/vm-base/tests/test_bootloader.py` — VM bootloader tests
- `base/images/vm-base/tests/test_kernel.py` — VM kernel tests
- `base/images/container-base/tests/test_container.py` — container-specific tests

### Existing files for reference (read-only)
- `base/images/images.toml` — image registry, maps names to KIWI paths
- `base/images/vm-base/vm-base.kiwi` — VM image definition (packages, partitions, bootloader config)
- `base/images/container-base/container-base.kiwi` — container image definition (minimal packages, entrypoint)
- `distro/azurelinux.distro.toml` — distro version info (release-ver: "4.0")

## Verification

1. **Unit test the parsers**: `uv run pytest image_test/ -k test_parse` — ensure parsers handle real file formats (include small inline test cases or a test_parsers.py)
2. **Dry run with a built vm-base image**: `cd base/images && uv run pytest --image-name vm-base --image-path ../out/images/vm-base/<image>.vhd -v` — all shared + vm-base tests should pass
3. **Dry run with container-base**: `cd base/images && uv run pytest --image-name container-base --image-path ../out/images/container-base/<image>.tar.xz -v` — shared tests pass, VM-only tests auto-skipped, container tests pass
4. **Verify collection filtering**: Run with `--image-name vm-base --collect-only` and confirm only `tests/` and `vm-base/tests/` are collected
5. **Verify missing image error**: Pass a nonexistent `--image-path` and confirm hard failure with clear error message
6. **Verify auto-skip**: Run container image and confirm partition/bootloader/kernel tests show as SKIPPED with reason

## Decisions

- **No KIWI XML parsing for expected values** — expected values come from TOML metadata (via `azldev config dump`), future lock files, and inline test assertions. Keeps tests decoupled from build definitions.
- **CLI tools over Python bindings** — `guestmount`, `guestunmount`, `virt-inspector`, `podman` called via subprocess. Avoids system site-packages pollution with uv.
- **Mounting over copying** — `guestmount --ro` (VM) and `podman image mount` (container) preserve full filesystem fidelity (permissions, ownership, symlinks, xattrs) and provide lazy I/O. Session yield-fixtures handle cleanup.
- **One image per invocation** — simplifies fixture lifecycle and avoids cross-image state. Run the suite twice for two images.
- **Auto-skip over explicit markers** — fixtures raise `pytest.skip()` when inapplicable to the image type. Optional `@pytest.mark.vm_only` / `@pytest.mark.container_only` markers available for documentation clarity but not required.
- **Session-scoped mounting** — mount happens once; all tests share the same mounted rootfs. Filesystem is mounted read-only.
- **Mountpoint inside project tree** — mountpoints go under `base/build/work/scratch/image-tests/` per repo conventions, not `/tmp`.

## Open Items / Future Extensions

1. **Auto-resolve image path from name** — future `--image-path` could be optional, defaulting to `base/out/images/<name>/` with glob for the image file
2. **Package lock files** — when `.lock` files are added next to KIWI definitions, add a test validating installed packages match the lock
3. **CI workflow** — designed for headless execution; adding a GHA workflow is a follow-up task
4. **Additional image types** — when new images are added, create `<name>/tests/` directory; shared tests run automatically

