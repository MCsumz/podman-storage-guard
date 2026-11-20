#!/bin/bash
# Real test of the Redis data check: a throwaway Redis writes an AOF into a temporary folder,
# zeros are written into the middle of it (as found on 20 Nov 2026 after a clean shutdown),
# and the guard's data check must detect it, back the folder up, repair it, and leave data
# that a fresh Redis loads. Never touches live data or running containers.
#   bash test_real_redis.sh <redis image pinned by digest>
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
IMAGE=${1:?usage: test_real_redis.sh <image@sha256:...>}
WORK=$(mktemp -d /tmp/storage-guard-redis.XXXXXX)
NAME=storage-guard-redis-test-$$
cleanup() { podman rm -f "$NAME" >/dev/null 2>&1; podman unshare rm -rf "$WORK" 2>/dev/null || rm -rf "$WORK"; }
trap cleanup EXIT
mkdir -p "$WORK/data/redis" "$WORK/backups" "$WORK/quadlets"
printf '[Container]\nImage=%s\n' "$IMAGE" > "$WORK/quadlets/test-redis.container"
step() { printf '%-40s %s\n' "$1" "$2"; }

# 1. Real AOF from a real Redis.
podman run -d --name "$NAME" --network none -v "$WORK/data/redis:/data" "$IMAGE" \
  redis-server --appendonly yes --appendfsync always >/dev/null
for i in $(seq 1 30); do podman exec "$NAME" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
for i in $(seq 1 4000); do echo "SET key:$i value-$i-$(printf '%0100d' 0)"; done | podman exec -i "$NAME" redis-cli >/dev/null
step "keys written" "$(podman exec "$NAME" redis-cli DBSIZE)"
podman stop -t 10 "$NAME" >/dev/null; podman rm "$NAME" >/dev/null
INCR=$(podman unshare sh -c "ls $WORK/data/redis/appendonlydir/*.incr.aof")
SIZE=$(podman unshare stat -c %s "$INCR")
step "incremental AOF" "$(basename "$INCR"), $SIZE bytes"

# 2. Damage it like 20 Nov: a run of zeros in the middle.
OFFSET=$((SIZE / 2))
podman unshare dd if=/dev/zero of="$INCR" bs=1 seek="$OFFSET" count=4096 conv=notrunc status=none
step "zeros written" "4096 bytes at offset $OFFSET"
podman run --rm --network none -v "$WORK/data/redis:/data" --entrypoint redis-check-aof "$IMAGE" \
  /data/appendonlydir/appendonly.aof.manifest >/dev/null 2>&1
step "redis-check-aof sees the damage" "exit $?"

# 3. The guard's data check on that folder.
python3 -B - "$HERE" "$WORK" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import podman_storage_guard as g
work = Path(sys.argv[2])
g.QUADLETS = work / 'quadlets'
settings = dict(g.DEFAULTS, backup_source=str(work / 'data'), backup_dir=str(work / 'backups'),
                data_checks=[{'kind': 'redis', 'unit': 'test-redis', 'path': str(work / 'data/redis')}])
report = {}
unresolved = g.data_checks(settings, report, running=[], backed_up=False)
result = report['data_checks'][0]
print('%-40s %s' % ('guard data check', result['state']))
print('%-40s %s' % ('problem it saw', result.get('problem', '-')[:90]))
print('%-40s %s' % ('backup before repair', 'yes, %d bytes' % report['backup']['bytes'] if 'backup' in report else 'NO'))
sys.exit(0 if result['state'] == 'repaired' and not unresolved and 'backup' in report else 1)
PY
GUARD=$?

# 4. A fresh Redis must load the repaired data.
podman run -d --name "$NAME" --network none -v "$WORK/data/redis:/data" "$IMAGE" redis-server --appendonly yes >/dev/null
LOADED=""
for i in $(seq 1 30); do LOADED=$(podman exec "$NAME" redis-cli DBSIZE 2>/dev/null) && [ -n "$LOADED" ] && break; sleep 1; done
step "fresh Redis loads repaired data" "${LOADED:-FAILED} keys"
podman logs "$NAME" 2>&1 | grep -E "DB loaded from append only file|Bad file format" | tail -1 | sed 's/^/  /'
tar -tzf "$WORK"/backups/*.tar.gz | grep -c 'incr.aof' | xargs printf '%-40s %s file(s)\n' "backup holds the damaged AOF"

[ "$GUARD" = 0 ] && [ -n "$LOADED" ] && echo "PASS" || { echo "FAIL"; exit 1; }
