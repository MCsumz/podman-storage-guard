#!/usr/bin/env python3
"""Boot guard for rootless Podman storage after an abrupt power-off.

A forced power-off on a filesystem without a journal (ext2, or a loop image on one) can leave
Podman's storage metadata half-written, and then no container starts until the storage is
rebuilt by hand. This systemd user unit runs before the Quadlet containers:

  start  Read and clear the clean-shutdown marker. Check Podman storage (quick after a clean
         shutdown, full after an abrupt one). After an abrupt reset, or when the check fails,
         take a cold backup of the configured data folder first. Then repair in stages:
         ``podman system check --repair --force``; re-pull missing pinned images; and if that
         is not enough, rebuild the storage (move it aside, keep the network definitions, pull
         every image pinned by digest in the Quadlet files).
  stop   Write the clean-shutdown marker (runs after the containers have stopped).
  check  Run the full check only and print the problems.

Nothing destructive happens while a guarded container is running, and nothing is repaired
without a finished, verified backup. Application data in bind mounts is never touched.
Settings: ~/.config/podman-storage-guard/config.json (see config.example.json).
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

HOME = Path.home()
CONFIG = HOME / '.config/podman-storage-guard/config.json'
STATE = HOME / '.local/state/podman-storage-guard'
MARKER = STATE / 'clean-shutdown'
REPORT = STATE / 'last-run.json'
HISTORY = STATE / 'history.jsonl'
STORAGE = HOME / '.local/share/containers/storage'
QUADLETS = HOME / '.config/containers/systemd'
DEFAULTS = {
    # Which Quadlet containers the guard protects: <name>.container -> <name>.service
    'quadlet_glob': '*.container',
    # The folder backed up before any repair, and where the backups go. Without both,
    # the guard only checks and reports.
    'backup_source': None,
    'backup_dir': None,
    # Optional command that must succeed before a backup, e.g. ["sh", "/path/check-disk.sh"].
    'backup_check': None,
    'keep_backups': 2,
    'keep_broken': 2,
}
PINNED = re.compile(r'^Image=(\S+@sha256:[0-9a-f]{64})\s*$', re.M)
# Storage metadata that must be valid JSON whenever it exists.
METADATA = ('overlay-layers/layers.json', 'overlay-layers/volatile-layers.json',
            'overlay-images/images.json', 'overlay-containers/containers.json',
            'overlay-containers/volatile-containers.json')
FS_ERRORS = re.compile(r'EXT[234]-fs (error|warning)|mounting unchecked fs|'
                       r'Structure needs cleaning|I/O error', re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def load_settings(path=None):
    settings = dict(DEFAULTS)
    try:
        value = json.loads(Path(path or CONFIG).read_text())
    except (OSError, ValueError):
        value = {}
    if isinstance(value, dict):
        settings.update({key: value[key] for key in DEFAULTS if key in value})
    return settings


def run(command, timeout=600):
    """Run a command; never raise for a non-zero exit."""
    try:
        # Podman echoes corrupt metadata bytes in its errors; never let decoding fail.
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, type(exc).__name__ + ': ' + str(exc)


def fsync_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def quadlets(settings):
    return sorted(QUADLETS.glob(settings['quadlet_glob']))


def units(settings):
    return [path.stem + '.service' for path in quadlets(settings)]


def pinned_images(settings):
    images = []
    for unit in quadlets(settings):
        images += PINNED.findall(unit.read_text())
    return images


def containers_running(settings):
    return [unit for unit in units(settings)
            if run(['systemctl', '--user', 'is-active', '--quiet', unit], 15)[0] == 0]


def kernel_fs_errors():
    code, output = run(['journalctl', '-k', '-b', '--no-pager', '-q', '-o', 'cat'], 60)
    return [line for line in output.splitlines() if FS_ERRORS.search(line)][:20] if code == 0 else []


def check_storage(settings, full):
    """Return a list of problems; empty means the storage looks healthy."""
    problems = []
    for relative in METADATA:
        path = STORAGE / relative
        if path.is_file():
            try:
                json.loads(path.read_bytes() or b'null')
            except (OSError, ValueError):
                problems.append('invalid JSON: ' + relative)
    database = STORAGE / 'db.sql'
    if database.is_file():
        try:
            with sqlite3.connect('file:%s?mode=ro' % database, uri=True, timeout=10) as db:
                verdict = db.execute('PRAGMA quick_check').fetchone()[0]
            if verdict != 'ok':
                problems.append('libpod database: ' + str(verdict)[:120])
        except sqlite3.Error as exc:
            problems.append('libpod database: ' + str(exc)[:120])
    code, output = run(['podman', 'system', 'check'] + ([] if full else ['--quick']), 1800)
    if code:
        problems.append('podman system check: ' + (output.splitlines() or ['exit %d' % code])[-1][:160])
    for image in pinned_images(settings):
        if run(['podman', 'image', 'exists', image], 60)[0]:
            problems.append('missing image: ' + image)
    return problems


def backup_prefix(source):
    return source.name + '-unclean-boot-'


def cold_backup(settings, report):
    """Back up the data folder before any repair; keep the newest few."""
    if not settings['backup_source'] or not settings['backup_dir']:
        raise RuntimeError('No backup configured (backup_source, backup_dir); refusing to repair without a backup')
    source, backups = Path(settings['backup_source']), Path(settings['backup_dir'])
    if not source.is_dir() or not backups.is_dir():
        raise RuntimeError('Backup source or target folder missing; refusing to repair without a backup')
    if settings['backup_check'] and run(list(settings['backup_check']), 30)[0]:
        raise RuntimeError('Backup check failed; refusing to repair without a backup')
    prefix = backup_prefix(source)
    target = backups / (prefix + datetime.now().strftime('%Y-%m-%d-%H%M%S') + '.tar.gz')
    partial = Path(str(target) + '.incomplete')
    # podman unshare maps files owned by container users back to readable ids.
    code, output = run(['podman', 'unshare', 'tar', '--one-file-system', '-czf', str(partial),
                        '-C', str(source.parent), source.name], 3600)
    if code not in (0, 1) or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise RuntimeError('Backup failed: ' + output[-160:])
    if run(['gzip', '-t', str(partial)], 1800)[0]:
        partial.unlink(missing_ok=True)
        raise RuntimeError('Backup failed its gzip check')
    digest = hashlib.sha256()
    with partial.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    os.replace(partial, target)
    fsync_write(Path(str(target) + '.sha256'), digest.hexdigest() + '  ' + target.name + '\n')
    report['backup'] = {'path': str(target), 'bytes': target.stat().st_size,
                        'sha256': digest.hexdigest(), 'tar_exit': code}
    for old in sorted(backups.glob(prefix + '*.tar.gz'))[:-int(settings['keep_backups'])]:
        old.unlink()
        Path(str(old) + '.sha256').unlink(missing_ok=True)


def rebuild_storage(settings, report):
    """Move the storage aside, keep the networks, re-pull every pinned image by digest."""
    images = pinned_images(settings)
    if not images:
        raise RuntimeError('No pinned images found in the Quadlet files')
    broken = STORAGE.with_name('storage.broken-' + datetime.now().strftime('%Y-%m-%d-%H%M%S'))
    networks = {}
    for path in sorted((STORAGE / 'networks').glob('*.json')):
        try:
            json.loads(path.read_text())
            networks[path.name] = path.read_text()
        except (OSError, ValueError):
            report.setdefault('dropped_networks', []).append(path.name)
    os.rename(STORAGE, broken)
    report['broken_storage'] = str(broken)
    STORAGE.mkdir(mode=0o700)
    (STORAGE / 'networks').mkdir(mode=0o700)
    for name, text in networks.items():
        fsync_write(STORAGE / 'networks' / name, text)
    pulled = []
    for image in images:
        code, output = run(['podman', 'pull', '--quiet', image], 1800)
        if code:
            # Without the images the new storage is no better; restore the old one for review.
            run(['podman', 'unshare', 'rm', '-rf', str(STORAGE)], 600)
            os.rename(broken, STORAGE)
            report.pop('broken_storage')
            raise RuntimeError('Pull failed for %s: %s' % (image, output[-120:]))
        pulled.append(image)
    report['pulled'] = pulled
    old = sorted(STORAGE.parent.glob('storage.broken-*'))
    for path in old[:-int(settings['keep_broken'])]:
        run(['podman', 'unshare', 'rm', '-rf', str(path)], 1800)
        report.setdefault('removed_broken', []).append(str(path))


def guard(settings=None):
    report = {'started_at': now(), 'host': os.uname().nodename, 'problems': []}
    clean = MARKER.is_file()
    MARKER.unlink(missing_ok=True)
    report['previous_shutdown'] = 'clean' if clean else 'abrupt'
    try:
        settings = settings or load_settings()
        if not clean:
            report['kernel_fs_errors'] = kernel_fs_errors()
        running = containers_running(settings)
        problems = check_storage(settings, full=not clean)
        report['problems'] = problems
        if not problems and clean:
            report['state'] = 'clean'
        elif running:
            # Never back up or repair under running containers; report only.
            report.update(state='needs_attention', running=running,
                          message='Problems found while containers were running; no repair attempted'
                          if problems else 'Abrupt reset detected while containers were running')
        else:
            cold_backup(settings, report)
            if not problems:
                report['state'] = 'checked_after_abrupt_reset'
            else:
                code, output = run(['podman', 'system', 'check', '--repair', '--force'], 3600)
                report['podman_repair'] = {'exit': code, 'output': output[-400:]}
                problems = check_storage(settings, full=True)
                missing = [p.split(': ', 1)[1] for p in problems if p.startswith('missing image: ')]
                if problems and len(missing) == len(problems):
                    # The storage itself is consistent; only images are gone. Pull them back first.
                    report['repulled'] = missing
                    for image in missing:
                        run(['podman', 'pull', '--quiet', image], 1800)
                    problems = check_storage(settings, full=True)
                if not problems:
                    report['state'] = 'repaired'
                else:
                    report['after_podman_repair'] = problems
                    rebuild_storage(settings, report)
                    problems = check_storage(settings, full=True)
                    report['after_rebuild'] = problems
                    report['state'] = 'rebuilt' if not problems else 'needs_attention'
    except (OSError, RuntimeError) as exc:
        report.update(state='needs_attention', message=str(exc)[:300])
    except Exception as exc:  # A guard bug must still leave a report and let the containers start.
        report.update(state='needs_attention', message='Guard error: %s: %s' % (type(exc).__name__, str(exc)[:240]))
    report['finished_at'] = now()
    fsync_write(REPORT, json.dumps(report, indent=2) + '\n')
    with HISTORY.open('a') as stream:
        stream.write(json.dumps(report, separators=(',', ':')) + '\n')
    print(json.dumps({k: report[k] for k in ('state', 'previous_shutdown', 'problems')}))
    return 0 if report['state'] != 'needs_attention' else 1


def mark_clean():
    fsync_write(MARKER, now() + '\n')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('action', choices=('start', 'stop', 'check'))
    action = parser.parse_args(argv).action
    if action == 'stop':
        return mark_clean()
    if action == 'check':
        problems = check_storage(load_settings(), full=True)
        print(json.dumps({'problems': problems}))
        return 1 if problems else 0
    return guard()


if __name__ == '__main__':
    sys.exit(main())
