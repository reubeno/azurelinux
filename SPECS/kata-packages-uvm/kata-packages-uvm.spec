Summary:        Metapackage for Kata UVM components
Name:           kata-packages-uvm
Version:        1.0.0
Release:        7%{?dist}
License:        MIT
Vendor:         Microsoft Corporation
Distribution:   Azure Linux
Group:          System Environment/Base
URL:            https://aka.ms/mariner

ExclusiveArch:  x86_64

Requires:       bash
Requires:       ca-certificates
Requires:       chrony
Requires:       cpio
Requires:       cryptsetup
Requires:       curl
Requires:       dbus
Requires:       elfutils-libelf
Requires:       filesystem
Requires:       grep
Requires:       gzip
Requires:       iptables
Requires:       iproute
Requires:       iputils
Requires:       irqbalance
Requires:       lvm2
Requires:       lz4
Requires:       procps-ng
Requires:       readline
Requires:       sed
# Note: We currently only support using systemd for our init process, not the kata-agent.
# When we go to add support for AGENT_INIT=yes, can drop this.
# https://github.com/microsoft/kata-containers/blob/msft-main/tools/osbuilder/rootfs-builder/cbl-mariner/config.sh#L10
Requires:       systemd
Requires:       tar
Requires:       tzdata
Requires:       util-linux
Requires:       zlib

%description
Metapackage to install the set of packages inside a Kata containers UVM

%package        coco
Summary:        Metapackage to install the set of packages inside a Kata confidential containers UVM.
Requires:       %{name} = %{version}-%{release}
Requires:       cifs-utils
Requires:       device-mapper
# Note: This assumes we are using systemd which may not always be the case when we support AGENT_INIT=yes
Requires:       systemd-udev

%description    coco

%package        build
Summary:        Metapackage to install the set of packages for building a Kata UVM.
Requires:       acpica-tools
Requires:       cargo
Requires:       clang
Requires:       kata-containers-tools
Requires:       kata-containers-cc-tools
Requires:       kernel-uvm
Requires:       kernel-uvm-devel
Requires:       make
Requires:       parted
# Python dependencies for non-packaged IGVM tool
Requires:       python3
Requires:       python3-pip
Requires:       python3-frozendict
Requires:       python3-ecdsa
Requires:       python3-pyelftools
Requires:       python3-cached_property
Requires:       python3-cstruct
Requires:       python3-devel
Requires:       python3-libs
Requires:       python3-setuptools
Requires:       python3-pytest
Requires:       python3-libclang
Requires:       python3-tomli
Requires:       qemu-img
Requires:       veritysetup

%description    build

%package        coco-sign
Summary:        Metapackage to install the set of packages for building the signing tool for Kata confidential containers UVM.
Requires:       build-essential
Requires:       golang

%description    coco-sign

%prep

%build

%files

%files coco

%files build

%files coco-sign

%changelog
* Mon Nov 25 2024 Manuel Huber <mahuber@microsoft.com> - 1.0.0-7
- Add explicit make dependency for UVM build
- Remove commented package dependencies

* Fri Sep 20 2024 Manuel Huber <mahuber@microsoft.com> - 1.0.0-6
- Update for 3.2.0.azl3 kata-containers(-cc) packages

* Wed Jun 19 2024 Cameron Baird <cameronbaird@microsoft.com> - 1.0.0-5
- Add explicit systemd dependencies for UVM

* Fri May 03 2024 Saul Paredes <saulparedes@microsoft.com> - 1.0.0-4
- Remove opa

* Thu Apr 11 2024 Archana Choudhary <archana1@microsoft.com> - 1.0.0-3
- Add cifs-utils to the list of dependencies

* Tue Feb 06 2024 Archana Choudhary <archana1@microsoft.com> - 1.0.0-2
- Remove dependency on kernel-uvm-cvm

* Tue Dec 19 2023 Mitch Zhu <mitchzhu@microsoft.com> - 1.0.0-1
- Introduce kata meta-package for the UVM components.
- License verified
- Original version for CBL-Mariner
