#!/bin/sh
# Installs the Linux boot switch on the multi-boot PC. Run as root from the repository root:
#   sudo sh deploy/linux-pc/install.sh
set -eu

REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need python3 "apt install python3"
need efibootmgr "apt install efibootmgr (UEFI one-time boot)"

if [ "$REPO_DIR" != /opt/cs2mmt ]; then
    rm -rf /opt/cs2mmt.new
    cp -a "$REPO_DIR" /opt/cs2mmt.new
    rm -rf /opt/cs2mmt
    mv /opt/cs2mmt.new /opt/cs2mmt
fi

install -d -m 700 /etc/cs2mmt
if [ ! -f /etc/cs2mmt/agent-linux.toml ]; then
    install -m 600 /opt/cs2mmt/config/agent-linux.example.toml /etc/cs2mmt/agent-linux.toml
    echo "edit /etc/cs2mmt/agent-linux.toml (controller URLs, api_token), then run this script again"
    exit 0
fi

echo "UEFI boot entries (the Windows one must match [linux] target):"
efibootmgr | grep -E '^Boot[0-9A-F]{4}' || true

install -m 644 /opt/cs2mmt/deploy/linux-pc/cs2mmt-boot-switch.service /etc/systemd/system/
install -m 644 /opt/cs2mmt/deploy/linux-pc/cs2mmt-boot-switch-watch.service /etc/systemd/system/
install -m 644 /opt/cs2mmt/deploy/linux-pc/cs2mmt-boot-switch-watch.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable cs2mmt-boot-switch.service
echo "enabled cs2mmt-boot-switch (runs at every boot)."
echo "optional: systemctl enable --now cs2mmt-boot-switch-watch.timer  (switch when the PC is left on in Linux)"

# Wake-on-LAN must stay armed after a Linux shutdown too.
for dev in /sys/class/net/*; do
    name=$(basename "$dev")
    [ "$name" = lo ] && continue
    [ -d "$dev/device" ] || continue
    if command -v ethtool >/dev/null 2>&1; then
        echo "$name: $(ethtool "$name" 2>/dev/null | grep -E 'Wake-on' | tr -s ' ' | tr '\n' ' ')"
    fi
done
echo "If 'Wake-on: d' is shown, enable it permanently, e.g. with NetworkManager:"
echo "  nmcli connection modify <connection> 802-3-ethernet.wake-on-lan magic"
