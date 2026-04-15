# Azure Linux Image Tests

Static validation framework for built Azure Linux images (VM and container). Mounts images read-only and runs pytest tests against the filesystem without booting.

## Prerequisites

System packages (not pip-installable):

- **`libguestfs-tools`** + **`guestfs-tools`** — provides `guestmount`, `guestunmount`, `virt-inspector` (VM images)
- **`skopeo`** — OCI archive conversion (container images)
- **`umoci`** — OCI image unpacking (container images)
- **`buildah`** — cleanup of rootless umoci extracts (container images)
- **`rpm`** — for `rpm --root` package queries
- **`uv`** — Python project/package manager

## Usage

```bash
cd base/images

# VM image
uv run pytest --image-name vm-base --image-path ../out/images/vm-base/<image>.raw

# Container image
uv run pytest --image-name container-base --image-path ../out/images/container-base/<image>.oci.tar.xz

# Collect only (verify test selection without running)
uv run pytest --image-name vm-base --image-path /path/to/image.raw --collect-only

# Verbose debug logging
uv run pytest --image-name vm-base --image-path /path/to/image.raw --log-cli-level=DEBUG
```

One image per invocation. The `--image-name` flag controls which per-image test directory is collected, while `common/tests/` (shared tests) always runs.

### Logging

The framework logs at INFO level by default (live CLI output). All fixture and helper code logs aggressively at DEBUG level. To see detailed extraction, parsing, and fixture resolution:

```bash
uv run pytest ... --log-cli-level=DEBUG
```

## Test Organization

```
base/images/
├── conftest.py              # CLI options, collection hooks, all fixtures
├── image_test/              # Helper package (not test-collected)
│   ├── extract.py           # Image mounting (guestmount / skopeo+umoci)
│   ├── disk.py              # VM disk inspection (virt-inspector)
│   ├── parsers.py           # File content parsers
│   └── types.py             # Dataclasses
├── common/tests/            # Shared tests (all images)
│   ├── test_os_release.py
│   ├── test_repos.py
│   ├── test_packages.py
│   ├── test_services.py
│   └── test_filesystem.py
├── vm-base/tests/           # VM-specific tests
│   ├── test_partitions.py
│   ├── test_bootloader.py
│   └── test_kernel.py
└── container-base/tests/    # Container-specific tests
    └── test_container.py
```

## How It Works

### VM images

`guestmount --ro` provides a read-only FUSE mount of the image filesystem with full fidelity (permissions, ownership, symlinks). `virt-inspector` extracts partition/filesystem metadata. Both use `LIBGUESTFS_BACKEND=direct` to avoid libvirt/SELinux issues.

### Container images

`skopeo copy` converts the OCI archive to an OCI layout, then `umoci unpack --rootless` extracts the rootfs — no root privileges or user namespaces required. Cleanup uses `buildah unshare rm -rf` to handle read-only directories preserved by the rootless extraction.

## Adding Tests

### Shared tests (all images)
Add to `common/tests/`. Use fixtures like `rootfs`, `os_release`, `installed_packages`, `file_stat_fn`.

### Image-specific tests
Add to `<image-name>/tests/`. These only run when `--image-name` matches. For VM-only logic, use `partition_table` or `kernel_cmdline` fixtures (they auto-skip for containers).

## Available Fixtures

| Fixture | Scope | Type | Description |
|---------|-------|------|-------------|
| `image_name` | session | `str` | From `--image-name` |
| `image_path` | session | `Path` | From `--image-path` |
| `image_type` | session | `str` | `"vm"` or `"container"` |
| `rootfs` | session | `Path` | Mounted image filesystem |
| `disk_info` | session | `DiskInfo \| None` | Partition info (VM only) |
| `os_release` | session | `dict[str, str]` | Parsed `/etc/os-release` |
| `installed_packages` | session | `set[str]` | Installed RPM names |
| `yum_repos` | session | `list[RepoInfo]` | Parsed repo files |
| `enabled_services` | session | `set[str]` | Enabled systemd services |
| `partition_table` | session | `list[PartitionInfo]` | Partitions (auto-skips for containers) |
| `kernel_cmdline` | session | `str` | GRUB cmdline (auto-skips for containers) |
| `file_stat_fn` | session | `Callable[[str], StatResult]` | Stat any file in the image |
