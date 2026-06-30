Summary:        Azure Linux modern Textual frontend for the anaconda installer
Name:           azl-installer-ui
Version:        0.1
Release:        1%{?dist}
License:        MIT
URL:            https://aka.ms/azurelinux

# Launcher script
Source0:        azl-anaconda

# Python package files — flat in SOURCES, mapped to azl_installer_ui/ at install
Source10:       ui_init.py
Source11:       ui_brand.py
Source12:       ui_dbus_helpers.py
Source13:       ui_app.py
Source14:       ui_interface.py

BuildArch:      noarch

BuildRequires:  python3-devel

Requires:       anaconda-core
Requires:       anaconda-tui
Requires:       python3
Requires:       python3-textual
Requires:       util-linux

%description
A modern Textual-based frontend for the anaconda installer used in the
Azure Linux ISO. Replaces the default simpleline TUI with a colored,
branded, keyboard- and mouse-friendly installer flow while reusing
anaconda's existing DBus install pipeline (BOSS / Storage / Users
modules).

Invoked via /usr/sbin/azl-anaconda, which monkey-patches anaconda's
InitInterface hook so that --text mode loads
azl_installer_ui.interface.AzlTextualInterface rather than the upstream
TextUserInterface.

%prep
# Nothing to unpack — sources are individual files.

%build
# Nothing to build (pure-Python package).

%install
install -d %{buildroot}%{python3_sitelib}/azl_installer_ui
install -m 0644 %{SOURCE10} %{buildroot}%{python3_sitelib}/azl_installer_ui/__init__.py
install -m 0644 %{SOURCE11} %{buildroot}%{python3_sitelib}/azl_installer_ui/brand.py
install -m 0644 %{SOURCE12} %{buildroot}%{python3_sitelib}/azl_installer_ui/dbus_helpers.py
install -m 0644 %{SOURCE13} %{buildroot}%{python3_sitelib}/azl_installer_ui/app.py
install -m 0644 %{SOURCE14} %{buildroot}%{python3_sitelib}/azl_installer_ui/interface.py

# Compile bytecode through the standard Python macros so the package
# owns both the .py and __pycache__/*.pyc files.
%py_byte_compile %{python3} %{buildroot}%{python3_sitelib}/azl_installer_ui

install -D -m 0755 %{SOURCE0} %{buildroot}%{_sbindir}/azl-anaconda

%files
%{python3_sitelib}/azl_installer_ui/
%{_sbindir}/azl-anaconda

%changelog
* Mon Jun 02 2026 Reuben Olinsky <reubeno@microsoft.com> - 0.1-1
- Initial version: Textual-based anaconda UserInterface frontend
