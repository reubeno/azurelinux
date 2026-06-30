#!/bin/bash
# KIWI config.sh — post-bootstrap configuration for Anaconda TUI Offline Installer ISO
set -euo pipefail

#----------------------------------------------------------------------
# Architecture detection — set arch-specific package and EFI names
#----------------------------------------------------------------------
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)
        GRUB_EFI_PKG="grub2-efi-x64"
        GRUB_EFI_MOD_PKG="grub2-efi-x64-modules"
        GRUB_EFI_CDBOOT_PKG="grub2-efi-x64-cdboot"
        SHIM_EFI="shimx64.efi"
        GRUB_EFI="grubx64.efi"
        BOOT_EFI="BOOTX64.EFI"
        ;;
    aarch64)
        GRUB_EFI_PKG="grub2-efi-aa64"
        GRUB_EFI_MOD_PKG="grub2-efi-aa64-modules"
        GRUB_EFI_CDBOOT_PKG="grub2-efi-aa64-cdboot"
        SHIM_EFI="shimaa64.efi"
        GRUB_EFI="grubaa64.efi"
        BOOT_EFI="BOOTAA64.EFI"
        ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac
echo "=== Architecture: $ARCH ==="
echo "  GRUB EFI package: $GRUB_EFI_PKG"
echo "  Shim EFI binary:  $SHIM_EFI"

#----------------------------------------------------------------------
# Variant detection
#----------------------------------------------------------------------
# kiwi sets `kiwi_profiles` to a comma-separated list of active profiles
# (one per build, set by --profile). The variant decides which
# `azurelinux-repos*` package goes into the image and the kickstart,
# which in turn controls the runtime repo of the installed system.
#
# OFFLINE_REPO_BLOCKLIST lists packages that must NOT appear in the
# offline repo. The opposite-variant repos package goes here:
# `azurelinux-repos` and `-dev` Conflict: with each other, and
# `azurelinux-release-common` has `Recommends: azurelinux-repos`, which
# would otherwise drag the canonical package into the dev offline repo
# via --resolve --alldeps and risk dnf picking the wrong one.
case ",${kiwi_profiles:-}," in
    *,vm-iso-installer-dev,*)
        AZL_REPOS_PKG="azurelinux-repos-dev"
        OFFLINE_REPO_BLOCKLIST=( "azurelinux-repos" )
        ;;
    *,vm-iso-installer,*)
        AZL_REPOS_PKG="azurelinux-repos"
        OFFLINE_REPO_BLOCKLIST=( "azurelinux-repos-dev" )
        ;;
    *)
        echo "ERROR: cannot determine variant from kiwi_profiles='${kiwi_profiles:-}'" >&2
        exit 1
        ;;
esac
echo "  Variant repos pkg:      $AZL_REPOS_PKG"
echo "  Offline repo blocklist: ${OFFLINE_REPO_BLOCKLIST[*]}"

#----------------------------------------------------------------------
# Download all target-install packages + deps for the offline repo
#----------------------------------------------------------------------
# During the ISO build we have network access to the repo.
# Download every package that anaconda will install on the target,
# including all transitive dependencies, into /opt/azl-offline-repo/.
# This lets the offline kickstart install from file:// with no network.

OFFLINE_REPO="/opt/azl-offline-repo"
mkdir -p "$OFFLINE_REPO"

# === Single source of truth for target-install packages ===
# These go into the kickstart %packages and the offline repo.
# Any package needed on the installed system belongs here.
INSTALL_PKGS=(
    bash
    coreutils
    systemd
    systemd-networkd
    systemd-resolved
    dnf5
    grub2
    "$GRUB_EFI_PKG"
    "$GRUB_EFI_MOD_PKG"
    shim
    efibootmgr
    kernel
    kernel-modules
    openssh-server
    openssh-clients
    sudo
    vim-minimal
    ca-certificates
    azurelinux-release
    "$AZL_REPOS_PKG"
    setup
    shadow-utils
    util-linux
    selinux-policy-targeted
    audit
    chrony
    cracklib-dicts
    glibc
    glibc-langpack-en
    cryptsetup
    firewalld
    iproute
)

# Extra packages needed in the offline repo for anaconda's runtime deps
# but not listed in kickstart %packages (pulled via --resolve --alldeps).
EXTRA_REPO_PKGS=(
    "$GRUB_EFI_CDBOOT_PKG"
    lvm2
    e2fsprogs
    dosfstools
    device-mapper-persistent-data
    mtools
    grub2-tools-extra
    libaio
)

echo "=== Downloading target-install packages + dependencies ==="
# Kiwi removes repo configs after package installation, so repos from the .kiwi
# file are NOT available during config.sh. Use --repofrompath with the CDN URL
# directly. Keep this URL in sync with the `azurelinux-base` repo in
# vm-iso-installer.kiwi.

AZL_BASE_URL="https://stcontroltowerdevjwisitg.blob.core.windows.net/azl4-dev/base/$ARCH"

EXCLUDE_ARGS=()
for pkg in "${OFFLINE_REPO_BLOCKLIST[@]}"; do
    EXCLUDE_ARGS+=( --exclude="$pkg" )
done

dnf5 download \
    --setopt=reposdir=/dev/null \
    --repofrompath=azl-base,"$AZL_BASE_URL" \
    --repo=azl-base \
    --resolve \
    --alldeps \
    --skip-unavailable \
    "${EXCLUDE_ARGS[@]}" \
    --destdir="$OFFLINE_REPO" \
    "${INSTALL_PKGS[@]}" "${EXTRA_REPO_PKGS[@]}" || {
    echo "WARNING: dnf download had errors — some packages may be missing"
}

RPM_COUNT=$(ls "$OFFLINE_REPO"/*.rpm 2>/dev/null | wc -l)
echo "=== Downloaded $RPM_COUNT RPMs ==="
ls "$OFFLINE_REPO"/*.rpm 2>/dev/null | head -20 || true
echo "..."

# Build repo metadata
createrepo_c "$OFFLINE_REPO"

#----------------------------------------------------------------------
# Validate offline repo completeness (dry-run install)
#----------------------------------------------------------------------
echo "=== Validating offline repo completeness ==="

# Verify that no blocklisted package landed in the offline repo.
# The `-[0-9]*` boundary disambiguates the package name from prefix
# overlap (e.g. `foo` vs `foo-bar`) since RPM filenames are
# `<name>-<version>-<release>.<arch>.rpm` with version starting in a digit.
for pkg in "${OFFLINE_REPO_BLOCKLIST[@]}"; do
    if ls "$OFFLINE_REPO/${pkg}"-[0-9]*.rpm >/dev/null 2>&1; then
        echo "!!!"
        echo "!!! FATAL: blocklisted package landed in offline repo:"
        echo "!!!   $(ls "$OFFLINE_REPO/${pkg}"-[0-9]*.rpm)"
        echo "!!!"
        echo "Fix: ensure the dnf5 download command excludes $pkg."
        exit 1
    fi
done

DRYRUN_ROOT=$(mktemp -d /tmp/azl-dryrun-XXXXXX)
DRYRUN_ERRORS=$(dnf5 install \
    --assumeno \
    --installroot="$DRYRUN_ROOT" \
    --releasever=4.0 \
    --setopt=reposdir=/dev/null \
    --repofrompath=offline,"file://$OFFLINE_REPO" \
    --repo=offline \
    "${INSTALL_PKGS[@]}" 2>&1) || true

rm -rf "$DRYRUN_ROOT"

if echo "$DRYRUN_ERRORS" | grep -qiE "No match for argument|nothing provides|cannot install"; then
    echo "!!!"
    echo "!!! FATAL: Offline repo is missing packages required by the kickstart!"
    echo "!!!"
    echo "$DRYRUN_ERRORS" | grep -iE "No match for argument|nothing provides|cannot install"
    echo ""
    echo "Fix: add the missing packages to INSTALL_PKGS."
    exit 1
else
    echo "=== Dry-run passed — all kickstart packages resolve from offline repo ==="
fi

echo "=== Offline repo ready at $OFFLINE_REPO ==="

#----------------------------------------------------------------------
# Anaconda launcher symlink (script deployed via kiwi <file>)
#----------------------------------------------------------------------
ln -sf /usr/local/bin/anaconda-launcher.sh /usr/local/bin/install-azl

#----------------------------------------------------------------------
# Plymouth — text theme only.
#
# We deliberately use only the `text` theme (shipped by the main plymouth
# package; no plymouth-graphics-libs / system-logos required) for a
# minimal, dependency-light cleaner boot sequence.
#
# IMPORTANT: the kernel cmdline must include `plymouth.ignore-serial-consoles`
# (set in vm-iso-installer.kiwi) — otherwise plymouth detects ttyS0 and
# silently forces the `details` theme instead of `text`, which echoes all
# systemd unit-status lines verbatim. See plymouth's ply-device-manager.c
# `create_devices_from_terminals()` for the detection logic.
#----------------------------------------------------------------------
if command -v plymouth-set-default-theme >/dev/null 2>&1; then
    plymouth-set-default-theme text || true
fi
# Sanity-check the default.plymouth symlink (it is what plymouthd actually
# loads). If it's wrong, plymouth silently falls back to `details`.
if [ -L /usr/share/plymouth/themes/default.plymouth ]; then
    echo "=== Plymouth default theme: $(readlink /usr/share/plymouth/themes/default.plymouth) ==="
else
    echo "WARN: /usr/share/plymouth/themes/default.plymouth is missing — forcing text"
    ln -sfn text/text.plymouth /usr/share/plymouth/themes/default.plymouth
fi
# Regenerate the initramfs so it bundles plymouth + the dracut module
# (the /etc/dracut.conf.d/10-plymouth.conf snippet from this image).
if command -v dracut >/dev/null 2>&1; then
    dracut --force --regenerate-all || \
        echo "WARN: dracut regenerate-all failed; plymouth may be absent from initramfs"
fi

#----------------------------------------------------------------------
# Welcome banner
#----------------------------------------------------------------------

cat > /root/.bash_profile << 'PROFILEEOF'
# Auto-install mode: if "azl.autoinstall" is on the kernel cmdline
# (selected via the "Install Azure Linux 4.0" GRUB entry), launch
# the installer on the first auto-login getty that completes.
#
# Race-safe: both tty1 and ttyS0 may auto-login simultaneously when both
# are wired up (typical for QEMU with both -nographic-style serial AND
# a VGA/VNC console). We use flock + a marker file so whichever getty
# fires first wins, and the other(s) fall through to the help banner.
if grep -q 'azl\.autoinstall' /proc/cmdline 2>/dev/null \
   && [ ! -f /run/azl-installer-aborted ]; then
    MY_TTY=$(tty 2>/dev/null)
    case "$MY_TTY" in
        /dev/tty1|/dev/tty0|/dev/ttyS0|/dev/ttyS1|/dev/hvc0)
            # First to grab the lock wins. flock -n returns immediately
            # with non-zero if someone else holds it.
            exec 9>/run/azl-installer.lock
            if flock -n 9 && [ ! -f /run/azl-installer-started ]; then
                : > /run/azl-installer-started
                exec /usr/local/bin/anaconda-launcher.sh
            fi
            ;;
    esac
fi
echo
echo "========================================"
echo "  Azure Linux 4.0 - Offline Installer"
echo "========================================"
echo
echo "  To start the installer, run:"
echo "    install-azl"
echo
echo "========================================"
echo
PROFILEEOF

cat > /root/.bashrc << 'RCEOF'
if [[ $- == *i* ]] && [ ! -f /tmp/.azl-banner-shown ]; then
    touch /tmp/.azl-banner-shown
    source /root/.bash_profile
fi
RCEOF

#----------------------------------------------------------------------
# Autologin on serial and VGA consoles
#----------------------------------------------------------------------

mkdir -p /etc/systemd/system/serial-getty@ttyS0.service.d
cat > /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf << 'AUTOEOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I 115200 linux
AUTOEOF

mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf << 'AUTOEOF'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I linux
AUTOEOF

#----------------------------------------------------------------------
# Generate kickstart files from templates (deployed via kiwi <file>)
#----------------------------------------------------------------------

# Helper: generate %packages section from INSTALL_PKGS array.
generate_packages_section() {
    echo "# Packages — minimal Azure Linux system"
    echo "# --nocore: AZL repo has no comps groups, so @core would fail"
    echo "%packages --nocore"
    for pkg in "${INSTALL_PKGS[@]}"; do
        echo "$pkg"
    done
    echo "%end"
}

# Expand @@PACKAGES@@ placeholder in each template
for ks_in in /root/azl-install.ks.in /root/azl-install-encrypted.ks.in; do
    ks_out="${ks_in%.in}"
    {
        sed '/@@PACKAGES@@/,$d' "$ks_in"
        generate_packages_section
        sed '1,/@@PACKAGES@@/d' "$ks_in"
    } > "$ks_out"
    rm -f "$ks_in"
done
