#!/bin/sh
# Install MiniDV Archiver as a systemd service on this host.
#
#   ./scripts/install.sh            single process (minidv-archive.service)
#   ./scripts/install.sh --split    grabber + converter + api + nginx UI
set -eu
test "$(id -u)" -eq 0 || { echo "Run as root" >&2; exit 1; }

SPLIT=0
[ "${1:-}" = "--split" ] && SPLIT=1

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

install -m 0644 "$SOURCE_DIR/systemd/99-minidv-firewire.rules" /etc/udev/rules.d/
udevadm control --reload
# apply the optional PCI power-management rule now (harmless if the IDs don't match)
udevadm trigger --subsystem-match=pci --action=add 2>/dev/null || true

if [ "$SPLIT" -eq 1 ]; then
  systemctl disable --now minidv-archive.service 2>/dev/null || true
  install -m 0644 "$SOURCE_DIR"/systemd/minidv-grabber.service \
                  "$SOURCE_DIR"/systemd/minidv-converter.service \
                  "$SOURCE_DIR"/systemd/minidv-api.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now minidv-grabber.service minidv-converter.service minidv-api.service
  if docker compose version >/dev/null 2>&1 && [ -f "$APP_DIR/compose.yaml" ]; then
    ( cd "$APP_DIR" && docker compose up -d )   # container nginx on :8088 proxying the api
    echo "UI via container: http://<host>:8088"
  elif [ -d /etc/nginx/sites-available ]; then
    install -m 0644 "$SOURCE_DIR/deploy/nginx-minidv.conf" /etc/nginx/sites-available/minidv
    ln -sf ../sites-available/minidv /etc/nginx/sites-enabled/minidv
    nginx -t && systemctl reload nginx
    echo "UI via bare-metal nginx (serves frontend/, proxies /api)"
  else
    echo "no nginx found — the api itself serves the UI on :${MINIDV_PORT:-8080}"
  fi
  echo "split deployment up: grabber + converter + api (:${MINIDV_PORT:-8080})"
else
  systemctl disable --now minidv-grabber.service minidv-converter.service minidv-api.service 2>/dev/null || true
  install -m 0644 "$SOURCE_DIR/systemd/minidv-archive.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now minidv-archive.service
  echo "up on http://$(hostname -I 2>/dev/null | awk '{print $1}'):${MINIDV_PORT:-8080}"
fi
