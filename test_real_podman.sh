#!/bin/bash
# Real-Podman test, run as the Podman user before installing. It uses a separate,
# throwaway storage (own graphroot/runroot via CONTAINERS_STORAGE_CONF) and never
# touches ~/.local/share/containers/storage or running containers.
#   bash test_real_podman.sh
# It writes the damage seen in practice (Redis data in volatile-layers.json) into the
# test storage and records what podman system check reports, whether --repair fixes it,
# and whether the guard's own check, backup and rebuild handle it. Needs internet for
# one small image pull.
set -uo pipefail
STAGE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d /tmp/storage-guard-realtest.XXXXXX)
TAG='docker.io/library/alpine:3.20'
cat > "$WORK/storage.conf" <<EOF
[storage]
driver = "overlay"
graphroot = "$WORK/graph"
runroot = "$WORK/run"
EOF
export CONTAINERS_STORAGE_CONF="$WORK/storage.conf"
cleanup() { podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"; }
trap cleanup EXIT
result() { printf '%-34s exit=%-3s %s\n' "$1" "$2" "$(printf '%s' "$3" | tr -d '\000' | tail -1 | cut -c1-140)"; }

real_root=$(unset CONTAINERS_STORAGE_CONF; podman info --format '{{.Store.GraphRoot}}')
test_root=$(podman info --format '{{.Store.GraphRoot}}')
echo "real graphroot: $real_root"
echo "test graphroot: $test_root"
[ "$test_root" = "$WORK/graph" ] || { echo "FAIL: test storage not isolated"; exit 1; }

out=$(podman pull --quiet "$TAG" 2>&1); result "pull test image" $? "$out"
IMAGE="docker.io/library/alpine@$(podman image inspect --format '{{.Digest}}' "$TAG")"
echo "pinned test image: $IMAGE"
out=$(podman system check 2>&1); result "check healthy storage" $? "$out"

layers="$WORK/graph/overlay-layers/volatile-layers.json"
[ -f "$layers" ] || layers="$WORK/graph/overlay-layers/layers.json"
echo "corrupting: ${layers#$WORK/graph/}"
printf 'REDIS0012\372\tredis-ver\0garbage' > "$layers"

out=$(podman system check --quick 2>&1); result "check --quick after damage" $? "$out"
out=$(podman system check 2>&1); result "check (full) after damage" $? "$out"
out=$(podman image exists "$IMAGE" 2>&1); result "image exists after damage" $? "$out"

out=$(python3 -B - "$STAGE" "$WORK/graph" <<'PY' 2>&1
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import podman_storage_guard as g
g.STORAGE = Path(sys.argv[2])
g.QUADLETS = Path('/nonexistent')
print(g.check_storage(g.DEFAULTS, full=False))
PY
); result "guard check_storage()" $? "$out"

out=$(podman system check --repair --force 2>&1 | tr -d '\000'); result "check --repair --force" $? "$out"
out=$(podman unshare true 2>&1 | tr -d '\000'); result "plain podman unshare (damaged)" ${PIPESTATUS[0]} "$out"

# The guard's own path on the damaged test storage: check, back up, rebuild, check again.
out=$(python3 -B - "$STAGE" "$WORK" "$IMAGE" <<'PY' 2>&1 | tr -d '\000'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import podman_storage_guard as g
work, image = Path(sys.argv[2]), sys.argv[3]
g.STORAGE = work / 'graph'
g.STATE = work / 'state'
g.QUADLETS = work / 'quadlets'
g.QUADLETS.mkdir()
(g.QUADLETS / 'test.container').write_text('[Container]\nImage=%s\n' % image)
source = work / 'app-data'
(source / 'pgdata').mkdir(parents=True)
(source / 'pgdata' / 'PG_VERSION').write_text('17\n')
(work / 'disk-ok.sh').write_text('exit 0\n')
(work / 'backups').mkdir()
settings = dict(g.DEFAULTS, backup_source=str(source), backup_dir=str(work / 'backups'),
                backup_check=['sh', str(work / 'disk-ok.sh')])
before = g.check_storage(settings, full=True)
print('problems before:', before)
report = {}
g.cold_backup(settings, report)
print('backup:', report['backup']['bytes'], 'bytes, tar exit', report['backup']['tar_exit'])
g.rebuild_storage(settings, report)
print('rebuilt: pulled', report['pulled'], 'broken kept:', Path(report['broken_storage']).name)
after = g.check_storage(settings, full=True)
print('problems after:', after)
sys.exit(0 if before and not after else 1)
PY
); status=$?; echo "$out" | sed 's/^/  guard: /'; echo "guard check→backup→rebuild→check exit=$status"
out=$(podman system check 2>&1); result "podman check after guard rebuild" $? "$out"
out=$(podman image exists "$IMAGE" 2>&1); result "image exists after guard rebuild" $? "$out"

after=$(unset CONTAINERS_STORAGE_CONF; podman ps --format '{{.Names}} {{.Status}}' | tr '\n' ';')
echo "real containers untouched: $after"
