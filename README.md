# Podman storage guard

[![Tests](https://github.com/MCsumz/podman-storage-guard/actions/workflows/tests.yml/badge.svg)](https://github.com/MCsumz/podman-storage-guard/actions/workflows/tests.yml)

A boot guard for **rootless Podman with Quadlet containers** on fragile storage. After every
boot, before your containers start, it checks Podman's storage. If the last shutdown was abrupt
or the storage is damaged, it backs up your data first and then repairs the storage in stages.

One Python file (standard library only), a systemd user unit, and a drop-in per container.

## Why

I run Nextcloud, PostgreSQL and Redis as Podman Quadlets on a PS4 running Linux. Its root
filesystem is an **unjournaled ext2** image, so a forced power-off (after a jailbreak freeze,
for example) can leave half-written files behind.

On 17 Nov 2026 that happened to Podman's own metadata: `overlay-layers/volatile-layers.json`
held a fragment of Redis data (`REDIS0012…`). No container would start, and the fix was a manual
rebuild: back up, move the storage aside, keep the network definitions, and pull every image
again by its digest. This guard does that by itself at the next boot, but only when it's needed,
and only once a backup exists.

## What it does

`podman-storage-guard.service` is a oneshot user unit. Each guarded container gets a drop-in
with `Wants=` and `After=` the guard. It isn't `Requires=`, so a guard failure never keeps a
container down.

1. **Clean or abrupt?** At every clean shutdown, after the containers have stopped, the unit
   writes a marker. At boot the guard reads and deletes it. No marker means the last shutdown was
   abrupt, and the kernel's filesystem errors from this boot are added to the report.
2. **Check:**
   - the storage metadata (`overlay-layers`, `overlay-images`, `overlay-containers`) is valid
     JSON
   - the libpod SQLite database passes `PRAGMA quick_check` (opened read-only)
   - `podman system check`: `--quick` after a clean shutdown, full after an abrupt one
   - every image pinned by digest in the Quadlet files exists
3. **Back up first.** After an abrupt shutdown, or when the check finds problems, it makes a cold
   backup of your data folder. It uses `podman unshare tar`, so files owned by container users
   are included. Each backup is gzip-tested and gets a `.sha256`. Only the newest two are kept,
   and other backups in that folder are never touched. An optional `backup_check` command (for
   example "is the backup disk mounted?") must pass first. **Nothing is repaired without a
   finished backup.**
4. **Repair in stages,** checking again after each:
   1. `podman system check --repair --force`
   2. if only pinned images are missing, pull them back by digest
   3. otherwise, rebuild the storage: move it to `storage.broken-<time>`, carry over the valid
      network definitions, and pull every pinned image by digest. If a pull fails (no internet,
      for example), the original storage is put back for review. The two newest broken copies
      are kept.
5. **Check application data, at every boot.** Damage doesn't only follow an abrupt shutdown: on
   an ext2 root, a Redis append-only file once held a run of zeros after a *clean* shutdown, and
   Redis crash-looped until it was fixed by hand. Each configured data check runs before its
   container starts:
   - **`redis`:** `redis-check-aof` (or `redis-check-rdb` for a folder with only `dump.rdb`) runs
     in a throwaway container from the image pinned in that container's Quadlet, with no network
     and the data folder mounted.
   - **`command`:** any check command, with an optional repair command.

   If a check fails and a repair exists, the guard makes the backup (if it hasn't already), runs
   the repair (`redis-check-aof --fix`, answering its prompt) and checks again. RDB has no repair
   tool, so a damaged `dump.rdb` is reported only. A container that is already running is skipped,
   because a live file can look damaged mid-write.
6. **Report:** `~/.local/state/podman-storage-guard/last-run.json`, plus `history.jsonl`. The
   states are:

| State | Meaning |
| --- | --- |
| `clean` | Clean shutdown, healthy storage |
| `checked_after_abrupt_reset` | Abrupt shutdown, backup taken, storage healthy |
| `repaired` | Fixed by `podman system check --repair` or by re-pulling images |
| `rebuilt` | Storage rebuilt from the pinned images |
| `data_repaired` | Storage healthy; a data check failed and its repair fixed it |
| `needs_attention` | Something needs a human; the report says what. The unit treats this as success, so the next clean shutdown is still recorded. |

### Safety

- **No repair under running containers:** if any guarded container is running, it only reports.
- **Application data changes only through a configured repair:** bind mounts and volumes are
  left alone, except that a failed data check runs its repair (such as `redis-check-aof --fix`),
  and only after a verified backup.
- **A guard bug still reports:** any unexpected error is caught and reported, and the containers
  still start.
- **Slow boots are fine:** `TimeoutStartSec=90min` absorbs a slow first Podman start, a backup and
  a re-pull.

## Install

Requirements:
- rootless Podman with Quadlet files in `~/.config/containers/systemd`
- the overlay storage driver, systemd and Python 3
- for a rebuild, images pinned by digest (`Image=…@sha256:…`)

1. Create `~/.config/podman-storage-guard/config.json`. Every key is optional:

   ```json
   {
     "quadlet_glob": "nc-*.container",
     "backup_source": "/home/me/nextcloud-production",
     "backup_dir": "/srv/nextcloud-data/backups/storage-guard",
     "backup_check": ["sh", "/home/me/nextcloud-production/check-hdd.sh"],
     "keep_backups": 2,
     "keep_broken": 2,
     "data_checks": [
       {"kind": "redis", "unit": "nc-redis", "path": "/home/me/nextcloud-production/redis"}
     ]
   }
   ```

   | Key | Default | Meaning |
   | --- | --- | --- |
   | `quadlet_glob` | `*.container` | Which Quadlet containers to guard |
   | `backup_source` | none | The data folder to back up before a repair |
   | `backup_dir` | none | Where backups go (must exist and be writable) |
   | `backup_check` | none | A command that must succeed before a backup |
   | `keep_backups`, `keep_broken` | `2` | How many guard backups and broken storages to keep |
   | `data_checks` | `[]` | Application data to check at every boot: `{"kind": "redis", "unit": "<Quadlet name>", "path": "<data folder>"}`, or `{"kind": "command", "name": "…", "check": [...], "repair": [...], "unit": "…"}` with `repair` and `unit` optional |

   Without `backup_source` and `backup_dir`, the guard checks and reports but never repairs.

2. Optionally, try the real-Podman test first. It uses its own throwaway storage, and your
   containers keep running:

   ```sh
   bash test_real_podman.sh
   ```

3. Install, as the Podman user, with no sudo:

   ```sh
   bash install.sh            # preflight: what would be installed
   bash install.sh --apply    # install, start, verify; rolls back on any failure
   bash install.sh --uninstall
   ```

   The installer checks `SHA256SUMS`, the config, the units and the backup folder. It installs the
   guard and one drop-in per container and starts the guard. It then requires state `clean`, the
   containers ordered after the guard, and running containers still running.

   For the guard to run at boot without a login, lingering must be on:
   `sudo loginctl enable-linger $USER`.

`python3 ~/.local/lib/podman-storage-guard/podman_storage_guard.py check` runs a full check and
the data checks by hand and prints the problems. It never repairs.

## Tests

**32 offline tests** use a fake `podman`, `systemctl` and `journalctl` with a temporary storage:

```sh
python3 -m unittest test_podman_storage_guard
```

They cover:
- a clean boot runs the quick check only
- an abrupt reset leads to a verified backup and a full check
- the real damage is fixed by `--repair`, with the backup taken before the repair
- unrepairable storage is rebuilt from pinned digests, keeping valid networks and dropping
  broken ones
- a failed pull restores the original storage
- no repair when the backup check fails, or when no backup is configured
- nothing destructive while containers run
- a missing image is re-pulled without a rebuild
- a corrupt libpod database is detected
- backup retention leaves manual backups alone
- a guard bug still writes a report
- the clean-shutdown marker cycle
- the settings: a missing, broken or partial config
- invalid UTF-8 in Podman's output (a regression test, below)
- data checks:
  - a valid AOF is only checked, with the pinned image and no network
  - a damaged AOF after a clean shutdown is backed up, fixed and rechecked, in that order
  - an unfixable AOF is reported without blocking the containers
  - no repair without a verified backup
  - one backup covers both a storage repair and a data repair
  - a running Redis is not checked
  - a `dump.rdb`-only folder is checked and reported, and an empty folder is fine
  - a clean `dump.rdb` never hides a damaged AOF, and an unreadable AOF folder is an error
    (regression tests, below)
  - the generic `command` kind, misconfigured entries, data checks waiting for usable storage,
    and `check` never repairing

**On real hardware, a PS4 running Linux with Podman 6.2.0, 18 Nov 2026:**
- `test_real_podman.sh` reproduced the damage in a throwaway storage.
  - **Detection works:** `podman system check` exits 125 with "loading primary layer store data…
    decode".
  - **`--repair --force` alone can't fix this kind of damage,** so the rebuild stage is the real
    fix.
  - **The guard's path works:** its check, backup and rebuild turned the damaged storage into a
    healthy one with the pinned image back. The real containers kept running throughout.
- **A bug the real test found:** Podman echoes the corrupt bytes in its error message, and the
  first run crashed with a `UnicodeDecodeError`. Command output is now decoded with
  `errors='replace'`.
- **Boot test:** a clean shutdown wrote the marker, and the next boot reported
  `previous_shutdown: clean` and `state: clean`. The containers started within 11 seconds after
  the guard, which had absorbed Podman's 3.5-minute first start after boot.

**Redis data check on the same PS4, 20 Nov 2026** (`test_real_redis.sh <redis image@sha256:…>`):
- A throwaway Redis wrote 4,000 keys with `appendonly yes`, then 4,096 zero bytes were written
  into the middle of its AOF, like the damage found that day. `redis-check-aof` exited 1.
- The guard reported "…incr.aof is not valid", made the backup (which holds the damaged AOF), ran
  `--fix` and rechecked: `repaired`. A fresh Redis loaded the repaired data: the 2,007 keys
  before the zeros, which is what `--fix` keeps. The production containers kept running.
- **A bug the real test found:** Redis's `appendonlydir` is mode 700 and owned by the container
  user, so a host-side search found no manifest. The guard then checked the clean `dump.rdb` and
  reported `ok` for a damaged AOF. It now lists the folder with `podman unshare ls`, and an
  unreadable AOF folder is an error, never a fallback to `dump.rdb`.

## Limits

- **It protects Podman's storage and the data you configure, not your filesystem.** On ext2 the
  real fix is an `e2fsck` and a journal (`tune2fs -j`). This guard keeps the containers
  recoverable meanwhile.
- **Data repairs can lose data:** `redis-check-aof --fix` keeps everything before the first bad
  entry and drops the rest. That suits a cache; for anything else, configure a check without a
  repair. Databases such as PostgreSQL aren't covered: they have no safe automatic fix for
  zeroed pages.
- **Rootless only:** it reads `~/.local/share/containers/storage`.
- **Rebuilds need digests:** images without one can't be restored by a rebuild and are skipped.
- **Built with AI assistance;** the integration and testing were done hands-on.

## License

[MIT](LICENSE)
