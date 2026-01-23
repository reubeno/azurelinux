#!/bin/bash
set -euxo pipefail

#
# NOTE: This script is a throwaway script. Please think ~~twice~~ thrice before you
# consider adding anything to it.
#

# Confirm working dir.
if [ ! -f azldev.toml ]; then
    echo "ERROR: This script must be run from the root of the repo" >&2
    exit 1
fi

# Check prereqs.
for prereq in azldev kiwi createrepo_c docker; do
    if ! command -v $prereq >/dev/null 2>&1; then
        echo "ERROR: Missing prerequisite '$prereq'." >&2
        exit 1
    fi
done

# 1. Build azurelinux-rpm-config to generate system macros, etc.
# 2. Build azurelinux-release and azurelinux-repos to provide repo files and release info.
#    These latter two require the rpm-config
# 3. Build rpm to ensure the azl-specific vendor tag is configured.
azldev comp build azurelinux-rpm-config  azurelinux-release azurelinux-repos rpm --publish-local-repo ./base/outazldev comp build rpm --local-repo ./base/out --publish-local-repo ./base/out
# Build a base container image using these private RPMs and upstream Fedora packages.
sudo kiwi --loglevel 10 \
    --kiwi-file container-base.kiwi \
    system build \
    --description ./base/images/container-base \
    --target-dir ./base/out/images \
    --add-repo="file:///$PWD/base/out,rpm-md,azl,1"

# Run a command in the container to verify.
xzcat ./base/out/images/azl4-container-base.x86_64-0.1.docker.tar.xz | docker load
docker run -it --rm microsoft/azurelinux/base/core:4.0 cat /etc/os-release
