#!/bin/bash
# Installs the Podman storage guard for the current (rootless Podman) user. No sudo, no restart.
#   bash install.sh             preflight only: shows what would be installed
#   bash install.sh --apply     install, start, and verify; rolls back on any failure
#   bash install.sh --uninstall remove the guard and its drop-ins; containers are untouched
# Create ~/.config/podman-storage-guard/config.json first (see config.example.json).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MODE=${1:-check}
LIB=$HOME/.local/lib/podman-storage-guard
UNITDIR=$HOME/.config/systemd/user
UNIT=podman-storage-guard.service
DROPIN=20-storage-guard.conf
CONFIG=$HOME/.config/podman-storage-guard/config.json
STATE=$HOME/.local/state/podman-storage-guard

uninstall() {
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$LIB/podman_storage_guard.py" "$UNITDIR/$UNIT" "$UNITDIR"/*.service.d/"$DROPIN"
  rmdir "$LIB" 2>/dev/null || true
  systemctl --user daemon-reload
  echo "guard removed; containers untouched; reports kept in $STATE"
}

if [ "$MODE" = --uninstall ]; then uninstall; exit 0; fi
[ "$MODE" = check ] || [ "$MODE" = --apply ] || { echo "usage: bash install.sh [--apply | --uninstall]" >&2; exit 2; }

(cd "$HERE" && sha256sum -c --quiet SHA256SUMS)
python3 -B -m py_compile "$HERE/podman_storage_guard.py"
rm -rf "$HERE/__pycache__"
[ -f "$CONFIG" ] || { echo "create $CONFIG first (see config.example.json)"; exit 1; }

# The guarded units and the backup target, as the guard itself reads them.
read -r -a UNITS <<< "$(python3 -B -c 'import sys
sys.path.insert(0, sys.argv[1])
import podman_storage_guard as g
print(" ".join(g.units(g.load_settings())))' "$HERE")"
BACKUP_DIR=$(python3 -B -c 'import sys
sys.path.insert(0, sys.argv[1])
import podman_storage_guard as g
print(g.load_settings()["backup_dir"] or "")' "$HERE")
[ "${#UNITS[@]}" -gt 0 ] || { echo "no Quadlet files match quadlet_glob in $CONFIG"; exit 1; }
for u in "${UNITS[@]}"; do systemctl --user cat "$u" >/dev/null; done
if [ -n "$BACKUP_DIR" ]; then
  [ -d "$BACKUP_DIR" ] && [ -w "$BACKUP_DIR" ] || { echo "backup_dir $BACKUP_DIR must exist and be writable"; exit 1; }
else
  echo "note: no backup configured - the guard will check and report, but never repair"
fi
podman system check --help >/dev/null
systemd-analyze --user verify "$HERE/$UNIT" 2>&1 | grep -v '^$' || true
[ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" = yes ] ||
  echo "note: lingering is off, so user units only start at login (sudo loginctl enable-linger $(id -un))"
running=()
for u in "${UNITS[@]}"; do [ "$(systemctl --user is-active "$u")" = active ] && running+=("$u"); done
echo "preflight ok: guarding ${UNITS[*]} (running: ${running[*]:-none})"
[ "$MODE" = --apply ] || exit 0

trap 'echo "install failed; rolling back"; uninstall' ERR
install -D -m 0644 "$HERE/podman_storage_guard.py" "$LIB/podman_storage_guard.py"
install -D -m 0644 "$HERE/$UNIT" "$UNITDIR/$UNIT"
for u in "${UNITS[@]}"; do install -D -m 0644 "$HERE/storage-guard.conf" "$UNITDIR/$u.d/$DROPIN"; done
# The system is up after a clean boot now; record that, so the first start takes the quick path.
python3 -B "$LIB/podman_storage_guard.py" stop
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

state=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["state"])' "$STATE/last-run.json")
[ "$state" = clean ] || { echo "first run state is $state, expected clean (see $STATE/last-run.json)"; false; }
[ "$(systemctl --user is-active "$UNIT")" = active ]
for u in "${UNITS[@]}"; do systemctl --user show "$u" -p After | grep -q "$UNIT"; done
for u in "${running[@]}"; do [ "$(systemctl --user is-active "$u")" = active ]; done
trap - ERR
echo "installed and verified: guard active (state clean), ordered before ${UNITS[*]}"
