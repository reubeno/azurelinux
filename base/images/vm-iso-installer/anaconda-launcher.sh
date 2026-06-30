#!/bin/bash
# Azure Linux 4.0 ISO installer entry point.
#
# Hands control to the modern Textual UI (`azl-anaconda`), which
# monkey-patches anaconda's UserInterface selection so that --text mode
# launches azl_installer_ui.interface.AzlTextualInterface instead of the
# upstream simpleline TextUserInterface.

set -u

mkdir -p /run/install

# Clean up stale PID file from any previous run.
rm -f /run/user/0/anaconda.pid

# --- Plymouth diagnostics ---
# Drop a report into /tmp so we can inspect post-mortem (visible from the
# Drop-to-shell path of the installer).
{
    echo "=== plymouth diagnostics @ $(date) ==="
    echo "--- /proc/cmdline ---"
    cat /proc/cmdline
    echo
    echo "--- /sys/class/tty/console/active ---"
    cat /sys/class/tty/console/active 2>/dev/null || echo "(missing)"
    echo
    echo "--- default.plymouth ---"
    ls -l /usr/share/plymouth/themes/default.plymouth 2>/dev/null || echo "(missing)"
    echo
    echo "--- plymouth --ping ---"
    plymouth --ping 2>&1 && echo OK || echo FAIL
    echo
    echo "--- plymouth-set-default-theme ---"
    plymouth-set-default-theme 2>&1 || true
    echo
    echo "--- /var/log/plymouth/debug.log (tail) ---"
    tail -80 /var/log/plymouth/debug.log 2>/dev/null || echo "(no debug log)"
    echo
    echo "--- journalctl -b -u plymouth-start.service ---"
    journalctl -b -u plymouth-start.service --no-pager 2>&1 | tail -40
    echo "=== end plymouth diagnostics ==="
} > /tmp/plymouth-diag.log 2>&1

# If plymouth is still showing a splash, tell it we're done so it relinquishes
# the VT cleanly before Textual takes the alt-screen buffer. --retain-splash
# leaves the last frame in place so there's no flash to black during handoff.
if command -v plymouth >/dev/null 2>&1 && plymouth --ping 2>/dev/null; then
    plymouth display-message --text="Starting Azure Linux installer..." || true
    sleep 1
    plymouth quit --retain-splash || true
fi

# Disable all system repos so anaconda only uses the offline repo bundled
# on the ISO. Prevents silent network fetches even when a NIC is present.
for repo_file in /etc/yum.repos.d/*.repo; do
    [ -f "$repo_file" ] && sed -i -E 's/^enabled\s*=\s*.*/enabled=0/' "$repo_file"
done

# Pick the kickstart: kernel-cmdline override wins; otherwise default to
# the bundled standard (non-encrypted) kickstart. Encryption support will
# move into the Textual UI in a follow-up.
KS=""
if grep -qo 'inst\.ks=[^ ]*' /proc/cmdline 2>/dev/null; then
    KS=$(grep -o 'inst\.ks=[^ ]*' /proc/cmdline | sed 's/inst\.ks=//')
else
    cp /root/azl-install.ks /run/install/ks.cfg
    KS=/run/install/ks.cfg
fi

# Textual switches to its alternate-screen buffer and clears on its own,
# so we don't need (or have) `clear` available in the ISO.
#
# Note: stderr is intentionally left on the console — if azl-anaconda
# crashes before Textual takes over the alt-screen, we want the
# traceback visible. anaconda's own logs still go to /tmp/anaconda.log.
echo
echo "Starting Azure Linux installer (anaconda is loading; this can take 20-60s)..."
echo
/usr/sbin/azl-anaconda --text --kickstart="$KS"
RC=$?

rm -f /run/install/ks.cfg

if [ $RC -eq 0 ]; then
    # azl-anaconda exits 0 on success; the kickstart's `reboot --eject`
    # handles the reboot itself, so we should never get here unless the
    # user picked "Reboot" without auto-reboot.
    echo
    echo "Installation complete. If the system did not reboot automatically,"
    echo "run: systemctl reboot"
    # Suppress .bash_profile auto-relaunch in any subsequent login on
    # this getty (systemd may respawn it and we don't want to loop).
    touch /run/azl-installer-aborted
    exec /bin/bash --noprofile --norc
elif [ $RC -eq 2 ]; then
    # Convention from azl-anaconda's completion screen: 'Drop to shell'.
    echo
    echo "Dropping to shell as requested."
    echo "Installer logs: /tmp/anaconda.log, /tmp/packaging.log, /tmp/azl-installer.log"
    touch /run/azl-installer-aborted
    exec /bin/bash --noprofile --norc
else
    echo
    echo "Installer exited with code $RC."
    echo "Logs: /tmp/anaconda.log, /tmp/packaging.log, /tmp/azl-installer.log"
    touch /run/azl-installer-aborted
    exec /bin/bash --noprofile --norc
fi
