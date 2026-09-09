#!/bin/sh
# Install MiniDV Archiver as a systemd service on this host.
set -eu
test "$(id -u)" -eq 0 || { echo "Run as root" >&2; exit 1; }

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
APP_DIR=${MINIDV_APP_DIR:-/opt/minidv-archive}
STORAGE=${MINIDV_STORAGE:-/srv/minidv}

install -d "$APP_DIR" "$STORAGE"
if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
  cp -a "$SOURCE_DIR/minidv_archiver" "$SOURCE_DIR/frontend" "$SOURCE_DIR/pyproject.toml" "$APP_DIR/"
fi

# runtime config — edit this file, not the unit
[ -f /etc/minidv-archive.env ] || {
  cp "$SOURCE_DIR/.env.example" /etc/minidv-archive.env
  echo "wrote /etc/minidv-archive.env (from .env.example) — review it"
}

install -m 0644 "$SOURCE_DIR/systemd/minidv-archive.service" /etc/systemd/system/
install -m 0644 "$SOURCE_DIR/systemd/99-minidv-firewire.rules" /etc/udev/rules.d/
udevadm control --reload
# apply the optional PCI power-management rule now (harmless if the IDs don't match)
udevadm trigger --subsystem-match=pci --action=add 2>/dev/null || true

systemctl daemon-reload
systemctl enable --now minidv-archive.service
echo "up on http://$(hostname -I 2>/dev/null | awk '{print $1}'):${MINIDV_PORT:-8080}"
