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
cd base/images/tests

# VM image — shared + VM-specific tests
uv run pytest cases/ cases/vm-base/ --image-path /path/to/image.raw

# Container image — shared + container-specific tests
uv run pytest cases/ cases/container-base/ --image-path /path/to/image.oci.tar.xz

# Shared tests only
uv run pytest cases/ --image-path /path/to/image.raw

# Explicit image type (overrides auto-detection from file extension)
uv run pytest cases/ --image-path /path/to/image --image-type vm

# Custom workdir for mounts/extractions (default: system temp dir)
uv run pytest cases/ --image-path /path/to/image.raw --workdir /tmp/my-workdir

# Verbose debug logging
uv run pytest cases/ --image-path /path/to/image.raw --log-cli-level=DEBUG
```

Test collection is controlled via standard pytest positional arguments — pass the directories/files you want to run.

### Logging

The framework logs at INFO level by default (live CLI output). All fixture and helper code logs aggressively at DEBUG level. To see detailed extraction, parsing, and fixture resolution:

```bash
uv run pytest ... --log-cli-level=DEBUG
```

## Test Organization

```
base/images/
├── images.toml                          # Image registry
├── vm-base/vm-base.kiwi                 # VM image definition
├── container-base/container-base.kiwi   # Container image definition
└── tests/
    ├── pyproject.toml                   # uv project: pytest, dependencies
    ├── conftest.py                      # Fixtures
    ├── utils/                           # Helper package (not test-collected)
    │   ├── pytest_plugin.py             # CLI options (loaded early via entry point)
    │   ├── extract.py                   # Image mounting (guestmount / skopeo+umoci)
    │   ├── disk.py                      # VM disk inspection (virt-inspector)
    │   ├── parsers.py                   # File content parsers
    │   └── types.py                     # Dataclasses
    └── cases/                           # Test cases
        ├── test_os_release.py           # Shared: /etc/os-release validation
        ├── test_repos.py                # Shared: yum repo validation
        ├── test_packages.py             # Shared: package validation
        ├── test_filesystem.py           # Shared: file permissions validation
        ├── vm-base/                     # VM-specific tests
        │   ├── test_partitions.py
        │   ├── test_bootloader.py
        │   └── test_kernel.py
        └── container-base/              # Container-specific tests
            └── test_container.py
```

## How It Works

### VM images

`guestmount --ro` provides a read-only FUSE mount of the image filesystem with full fidelity (permissions, ownership, symlinks). `virt-inspector` extracts partition/filesystem metadata. Both use `LIBGUESTFS_BACKEND=direct` to avoid libvirt/SELinux issues.

### Container images

`skopeo copy` converts the OCI archive to an OCI layout, then `umoci unpack --rootless` extracts the rootfs — no root privileges or user namespaces required. Cleanup uses `buildah unshare rm -rf` to handle read-only directories preserved by the rootless extraction.

### Image type detection

Image type is auto-detected from the file extension (`.raw`/`.vhd` → vm, `.oci.tar.xz` → container). Override with `--image-type vm|container` if needed.

## Adding Tests

### Shared tests (all images)
Add to `cases/`. Use fixtures like `rootfs`, `os_release`, `installed_packages`, `file_stat_fn`.

### Image-specific tests
Add to `cases/<image-name>/`. These only run when the caller includes that directory. VM-only fixtures (`partition_table`, `kernel_cmdline`) auto-skip for container images.

## CLI Options

| Option | Required | Description |
|--------|----------|-------------|
| `--image-path` | Yes | Path to the built image artifact |
| `--image-type` | No | `vm` or `container` (auto-detected from extension) |
| `--workdir` | No | Working directory for mounts/extractions (default: temp dir) |

## Available Fixtures

| Fixture | Scope | Type | Description |
|---------|-------|------|-------------|
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
