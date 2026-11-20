"""Offline tests for the Podman storage guard: fake podman/systemctl/journalctl, temporary storage.

Run: python3 -m unittest test_podman_storage_guard
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import podman_storage_guard as guard  # noqa: E402

IMAGES = ['docker.io/library/redis@sha256:' + 'a' * 64,
          'docker.io/library/postgres@sha256:' + 'b' * 64,
          'docker.io/library/nextcloud@sha256:' + 'c' * 64]


class FakeHost:
    """Stands in for podman, systemctl, journalctl, the backup check, tar and gzip."""

    def __init__(self, storage, source):
        self.storage = storage
        self.source = source
        self.calls = []
        self.running = set()
        self.images = set(IMAGES)
        self.check_fails = False        # podman system check exit status
        self.repair_fixes = False       # --repair clears check_fails
        self.pull_fails = False
        self.disk_ok = True
        self.kernel = 'EXT4-fs error (device loop0): ext4_mb_generate_buddy: group 1092\nnormal line'
        self.aof_valid = True           # redis-check-aof result
        self.aof_fixable = True         # --fix makes the AOF valid
        self.rdb_valid = True
        self.command_ok = True          # result of the generic "my-check" command
        self.unshare_ls_fails = False
        self.inputs = []                # (command, stdin) for commands that got input

    def __call__(self, command, timeout=600, input_text=None):
        self.calls.append(command)
        if input_text is not None:
            self.inputs.append((command, input_text))
        name = command[0]
        if command[:2] == ['podman', 'run']:
            tool = command[command.index('--entrypoint') + 1]
            if tool == 'redis-check-aof':
                if '--fix' in command:
                    if input_text != 'y\n':
                        return 1, 'Continue? [y/N]: Aborted'
                    self.aof_valid = self.aof_fixable
                    return 0, 'Successfully truncated AOF'
                return (0, 'All AOF files and manifest are valid') if self.aof_valid else \
                    (1, 'AOF appendonly.aof.11.incr.aof is not valid. Use the --fix option to try fixing it.')
            if tool == 'redis-check-rdb':
                return (0, 'RDB looks OK!') if self.rdb_valid else (1, '--- RDB ERROR DETECTED ---')
        if name == 'my-check':
            return (0, 'ok') if self.command_ok else (2, 'data broken')
        if name == 'my-repair':
            self.command_ok = True
            return 0, 'fixed'
        if name == 'systemctl':
            return (0 if command[-1] in self.running else 3), ''
        if name == 'journalctl':
            return 0, self.kernel
        if name == 'sh':
            return (0, 'disk verified') if self.disk_ok else (1, 'disk missing')
        if name == 'gzip':
            return 0, ''
        if command[:2] == ['podman', 'unshare']:
            if command[2] == 'tar':
                target = Path(command[command.index('-czf') + 1])
                with tarfile.open(target, 'w:gz') as archive:
                    archive.add(self.source, arcname=self.source.name)
                return 0, ''
            if command[2:4] == ['rm', '-rf']:
                shutil.rmtree(command[4], ignore_errors=True)
                return 0, ''
            if command[2:4] == ['ls', '-1']:
                if self.unshare_ls_fails:
                    return 2, "ls: cannot open directory '%s': Permission denied" % command[4]
                return 0, '\n'.join(sorted(p.name for p in Path(command[4]).iterdir()))
        if command[:3] == ['podman', 'system', 'check']:
            if '--repair' in command and self.repair_fixes:
                self.check_fails = False
                (self.storage / 'overlay-layers/volatile-layers.json').write_text('[]')
            return (1, 'Error: layer damaged') if self.check_fails else (0, '')
        if command[:3] == ['podman', 'image', 'exists']:
            return (0 if command[3] in self.images and self.storage.exists() else 1), ''
        if command[:2] == ['podman', 'pull']:
            if self.pull_fails:
                return 125, 'Error: dial tcp: no route to host'
            self.images.add(command[-1])
            return 0, ''
        raise AssertionError('unexpected command: %r' % command)


class GuardCase(unittest.TestCase):
    """Temporary home, storage, Quadlets and backup disk, with the fake host patched in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='storage-guard-')
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / 'home'
        storage = home / '.local/share/containers/storage'
        for folder in ('overlay-layers', 'overlay-images', 'overlay-containers', 'networks'):
            (storage / folder).mkdir(parents=True)
        (storage / 'overlay-layers/layers.json').write_text('[]')
        (storage / 'overlay-layers/volatile-layers.json').write_text('[]')
        (storage / 'overlay-images/images.json').write_text('[]')
        (storage / 'networks/app-net.json').write_text('{"name": "app-net"}')
        with closing(sqlite3.connect(storage / 'db.sql')) as db:
            db.execute('create table t (x)')
        quadlets = home / '.config/containers/systemd'
        quadlets.mkdir(parents=True)
        for image in IMAGES:
            name = image.split('/')[-1].split('@')[0]
            (quadlets / ('app-%s.container' % name)).write_text('[Container]\nImage=%s\n' % image)
        (quadlets / 'other.container').write_text('[Container]\nImage=docker.io/library/busybox\n')
        source = home / 'app-data'
        (source / 'postgres').mkdir(parents=True)
        (source / 'postgres/PG_VERSION').write_text('17\n')
        self.backups = Path(self.tmp.name) / 'disk/backups'
        self.backups.mkdir(parents=True)
        state = home / '.local/state/podman-storage-guard'
        self.paths = dict(HOME=home, CONFIG=home / '.config/podman-storage-guard/config.json',
                          STATE=state, MARKER=state / 'clean-shutdown',
                          REPORT=state / 'last-run.json', HISTORY=state / 'history.jsonl',
                          STORAGE=storage, QUADLETS=quadlets)
        for key, value in self.paths.items():
            item = patch.object(guard, key, value)
            item.start()
            self.addCleanup(item.stop)
        self.settings = dict(guard.DEFAULTS, quadlet_glob='app-*.container', backup_source=str(source),
                             backup_dir=str(self.backups), backup_check=['sh', str(home / 'check-disk.sh')])
        self.host = FakeHost(storage, source)
        item = patch.object(guard, 'run', side_effect=self.host)
        item.start()
        self.addCleanup(item.stop)
        self.storage = storage

    def guard(self):
        return guard.guard(self.settings)

    def clean_shutdown(self):
        self.assertEqual(guard.mark_clean(), 0)

    def report(self):
        return json.loads(self.paths['REPORT'].read_text())

    def corrupt_storage(self):
        # The real damage this was built for: Redis data in Podman's layer metadata.
        (self.storage / 'overlay-layers/volatile-layers.json').write_bytes(b'REDIS0012\xfa\tredis-ver')
        self.host.check_fails = True

    def podman_calls(self, *prefix):
        return [c for c in self.host.calls if c[:len(prefix)] == list(prefix)]


class GuardTests(GuardCase):
    def test_clean_boot_quick_check_only(self):
        self.clean_shutdown()
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual((report['state'], report['previous_shutdown']), ('clean', 'clean'))
        self.assertEqual(self.podman_calls('podman', 'system', 'check'), [['podman', 'system', 'check', '--quick']])
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertFalse(self.paths['MARKER'].exists(), 'marker is consumed at start')
        self.assertNotIn('kernel_fs_errors', report)

    def test_abrupt_reset_with_healthy_storage_backs_up_and_full_checks(self):
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual((report['state'], report['previous_shutdown']),
                         ('checked_after_abrupt_reset', 'abrupt'))
        self.assertEqual(self.podman_calls('podman', 'system', 'check'), [['podman', 'system', 'check']])
        self.assertEqual(report['kernel_fs_errors'], ['EXT4-fs error (device loop0): ext4_mb_generate_buddy: group 1092'])
        backup = Path(report['backup']['path'])
        self.assertTrue(backup.name.startswith('app-data-unclean-boot-'))
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), report['backup']['sha256'])
        self.assertEqual(Path(str(backup) + '.sha256').read_text().split(), [report['backup']['sha256'], backup.name])
        with tarfile.open(backup) as archive:
            self.assertIn('app-data/postgres/PG_VERSION', archive.getnames())
        self.assertFalse(any(p.name.endswith('.incomplete') for p in self.backups.iterdir()))

    def test_damage_fixed_by_podman_repair_after_the_backup(self):
        self.corrupt_storage()
        self.host.repair_fixes = True
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual(report['state'], 'repaired')
        self.assertIn('invalid JSON: overlay-layers/volatile-layers.json', report['problems'])
        self.assertIn('backup', report)
        order = [c[:4] for c in self.host.calls if c[0] == 'podman' and c[1] in ('unshare', 'system')]
        self.assertLess(order.index(['podman', 'unshare', 'tar', '--one-file-system']),
                        order.index(['podman', 'system', 'check', '--repair']), 'backup before repair')
        self.assertTrue(self.storage.exists())
        self.assertEqual(self.podman_calls('podman', 'pull'), [])

    def test_unrepairable_storage_is_rebuilt_from_pinned_digests(self):
        self.corrupt_storage()
        (self.storage / 'networks/broken.json').write_text('{not json')
        original_check = self.host.__call__

        def after_rebuild_healthy(command, timeout=600):
            if command[:3] == ['podman', 'system', 'check'] and not (self.storage / 'overlay-layers').exists():
                return 0, ''
            return original_check(command, timeout)

        with patch.object(guard, 'run', side_effect=after_rebuild_healthy):
            self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual(report['state'], 'rebuilt')
        self.assertEqual(sorted(report['pulled']), sorted(IMAGES), 'only images pinned by digest')
        broken = Path(report['broken_storage'])
        self.assertTrue((broken / 'overlay-layers/volatile-layers.json').read_bytes().startswith(b'REDIS'))
        self.assertEqual(json.loads((self.storage / 'networks/app-net.json').read_text())['name'], 'app-net')
        self.assertFalse((self.storage / 'networks/broken.json').exists())
        self.assertEqual(report['dropped_networks'], ['broken.json'])
        self.assertEqual(report['after_rebuild'], [])

    def test_pull_failure_restores_the_original_storage(self):
        self.corrupt_storage()
        self.host.pull_fails = True
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual(report['state'], 'needs_attention')
        self.assertIn('Pull failed', report['message'])
        self.assertTrue((self.storage / 'overlay-layers/volatile-layers.json').read_bytes().startswith(b'REDIS'))
        self.assertEqual(list(self.storage.parent.glob('storage.broken-*')), [])
        self.assertNotIn('broken_storage', report)

    def test_no_repair_when_the_backup_check_fails(self):
        self.corrupt_storage()
        self.host.disk_ok = False
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual(report['state'], 'needs_attention')
        self.assertIn('refusing to repair without a backup', report['message'])
        self.assertEqual(self.podman_calls('podman', 'system', 'check', '--repair', '--force'), [])

    def test_no_repair_without_a_configured_backup(self):
        self.corrupt_storage()
        self.settings.update(backup_source=None, backup_dir=None, backup_check=None)
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual(report['state'], 'needs_attention')
        self.assertIn('No backup configured', report['message'])
        self.assertIn('invalid JSON: overlay-layers/volatile-layers.json', report['problems'])
        self.assertEqual(self.podman_calls('podman', 'unshare'), [])
        self.assertEqual(self.podman_calls('podman', 'system', 'check', '--repair', '--force'), [])

    def test_nothing_destructive_while_containers_run(self):
        self.corrupt_storage()
        self.host.running = {'app-postgres.service'}
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual((report['state'], report['running']), ('needs_attention', ['app-postgres.service']))
        self.assertEqual(self.podman_calls('podman', 'unshare'), [])
        self.assertEqual(self.podman_calls('podman', 'system', 'check', '--repair', '--force'), [])

    def test_install_time_start_with_running_containers_is_harmless(self):
        self.clean_shutdown()
        self.host.running = set(guard.units(self.settings))
        self.assertEqual(self.guard(), 0)
        self.assertEqual(self.report()['state'], 'clean')

    def test_missing_pinned_image_is_pulled_without_a_rebuild(self):
        self.clean_shutdown()
        self.host.images.discard(IMAGES[2])
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertIn('missing image: ' + IMAGES[2], report['problems'])
        self.assertEqual((report['state'], report['repulled']), ('repaired', [IMAGES[2]]))
        self.assertNotIn('broken_storage', report)
        self.assertIn('backup', report)
        self.assertEqual(self.podman_calls('podman', 'pull'), [['podman', 'pull', '--quiet', IMAGES[2]]])

    def test_corrupt_libpod_database_is_reported(self):
        self.clean_shutdown()
        (self.storage / 'db.sql').write_bytes(b'not a database at all' * 100)
        problems = guard.check_storage(self.settings, full=False)
        self.assertTrue(any(p.startswith('libpod database:') for p in problems), problems)

    def test_only_the_newest_unclean_boot_backups_are_kept(self):
        for stamp in ('2026-11-01-100000', '2026-11-02-100000', '2026-11-03-100000'):
            old = self.backups / ('app-data-unclean-boot-%s.tar.gz' % stamp)
            old.write_bytes(b'old')
            Path(str(old) + '.sha256').write_text('x  ' + old.name)
        manual = self.backups / 'app-data-2026-11-17-manual.tar.gz'
        manual.write_bytes(b'manual')
        self.guard()
        names = sorted(p.name for p in self.backups.glob('app-data-unclean-boot-*.tar.gz'))
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], 'app-data-unclean-boot-2026-11-03-100000.tar.gz')
        self.assertTrue(manual.exists(), 'manual backups are never pruned')

    def test_guard_bug_still_writes_a_report(self):
        self.clean_shutdown()
        with patch.object(guard, 'check_storage', side_effect=KeyError('boom')):
            self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual(report['state'], 'needs_attention')
        self.assertIn('Guard error: KeyError', report['message'])

    def test_marker_cycle_and_history(self):
        self.guard()
        self.clean_shutdown()
        self.guard()
        lines = [json.loads(line) for line in self.paths['HISTORY'].read_text().splitlines()]
        self.assertEqual([entry['previous_shutdown'] for entry in lines], ['abrupt', 'clean'])

    def test_quadlet_glob_selects_the_guarded_containers(self):
        self.assertEqual(guard.units(self.settings),
                         ['app-nextcloud.service', 'app-postgres.service', 'app-redis.service'])
        everything = dict(self.settings, quadlet_glob='*.container')
        self.assertEqual(len(guard.units(everything)), 4)
        self.assertEqual(sorted(guard.pinned_images(everything)), sorted(IMAGES), 'unpinned images are skipped')


class SettingsTests(unittest.TestCase):
    def test_missing_broken_and_partial_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            self.assertEqual(guard.load_settings(path), guard.DEFAULTS)
            path.write_text('{not json')
            self.assertEqual(guard.load_settings(path), guard.DEFAULTS)
            path.write_text(json.dumps({'backup_dir': '/backups', 'unknown': 1}))
            settings = guard.load_settings(path)
            self.assertEqual(settings['backup_dir'], '/backups')
            self.assertNotIn('unknown', settings)
            self.assertEqual(settings['quadlet_glob'], '*.container')

    def test_example_config_loads(self):
        example = Path(__file__).resolve().parent / 'config.example.json'
        settings = guard.load_settings(example)
        self.assertEqual(set(settings), set(guard.DEFAULTS))
        self.assertEqual(json.loads(example.read_text()).keys() - set(guard.DEFAULTS), set())


class RunTests(unittest.TestCase):
    def test_invalid_utf8_from_a_command_does_not_crash(self):
        # Found on real hardware: podman echoes the corrupt metadata bytes in its error.
        code, output = guard.run([sys.executable, '-c',
                                  'import sys; sys.stderr.buffer.write(b"REDIS0012\\xfa\\tbad"); sys.exit(125)'])
        self.assertEqual(code, 125)
        self.assertIn('REDIS0012', output)


class DataCheckTests(GuardCase):
    """Application data checks; the 20 Nov 2026 case was a Redis AOF with zeros after a clean shutdown."""

    def setUp(self):
        super().setUp()
        self.redis = Path(self.settings['backup_source']) / 'redis'
        (self.redis / 'appendonlydir').mkdir(parents=True)
        (self.redis / 'appendonlydir/appendonly.aof.manifest').write_text('file appendonly.aof.11.incr.aof seq 11 type i\n')
        self.settings['data_checks'] = [{'kind': 'redis', 'unit': 'app-redis', 'path': str(self.redis)}]

    def redis_runs(self, flag=None):
        runs = [c for c in self.host.calls if c[:2] == ['podman', 'run']]
        return [c for c in runs if flag in c] if flag else runs

    def test_valid_aof_on_a_clean_boot_is_only_checked(self):
        self.clean_shutdown()
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual(report['state'], 'clean')
        self.assertEqual(report['data_checks'], [{'name': 'app-redis', 'state': 'ok'}])
        (check,) = self.redis_runs()
        self.assertIn(IMAGES[0], check, 'uses the image pinned in the Quadlet')
        self.assertEqual(check[check.index('--network') + 1], 'none')
        self.assertIn('%s:/data' % self.redis, check)
        self.assertEqual(check[-1], '/data/appendonlydir/appendonly.aof.manifest')
        self.assertEqual(list(self.backups.iterdir()), [], 'no backup when nothing is repaired')

    def test_damaged_aof_after_a_clean_shutdown_is_backed_up_then_fixed(self):
        self.clean_shutdown()
        self.host.aof_valid = False
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual(report['state'], 'data_repaired')
        result = report['data_checks'][0]
        self.assertEqual(result['state'], 'repaired')
        self.assertIn('not valid', result['problem'])
        self.assertIn('backup', report)
        order = [c[2] if c[1] == 'unshare' else ('fix' if '--fix' in c else 'check')
                 for c in self.host.calls if c[0] == 'podman' and c[1] in ('unshare', 'run')]
        self.assertEqual(order, ['ls', 'check', 'tar', 'fix', 'check'], 'backup before the fix, recheck after')
        (fix,) = self.redis_runs('--fix')
        self.assertIn(('y\n'), [stdin for cmd, stdin in self.host.inputs if cmd == fix])

    def test_clean_dump_rdb_never_hides_a_damaged_aof(self):
        # Found on the PS4: appendonlydir is mode 700 for the container user, so a host-side
        # glob saw nothing and the guard checked the clean dump.rdb instead, reporting "ok".
        (self.redis / 'dump.rdb').write_bytes(b'REDIS0012')
        self.clean_shutdown()
        self.host.aof_valid = False
        self.assertEqual(self.guard(), 0)
        self.assertEqual(self.report()['data_checks'][0]['state'], 'repaired')
        self.assertTrue(all('redis-check-aof' in c for c in self.redis_runs()))
        self.assertEqual(self.podman_calls('podman', 'unshare', 'ls', '-1'),
                         [['podman', 'unshare', 'ls', '-1', str(self.redis / 'appendonlydir')]])

    def test_unreadable_aof_folder_is_an_error_not_ok(self):
        (self.redis / 'dump.rdb').write_bytes(b'REDIS0012')
        self.clean_shutdown()
        self.host.unshare_ls_fails = True
        self.assertEqual(self.guard(), 1)
        state = self.report()['data_checks'][0]['state']
        self.assertTrue(state.startswith('error: cannot find the AOF manifest'), state)
        self.assertEqual(self.redis_runs(), [])

    def test_unfixable_aof_needs_attention_but_does_not_block(self):
        self.clean_shutdown()
        self.host.aof_valid = self.host.aof_fixable = False
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertEqual(report['state'], 'needs_attention')
        self.assertEqual(report['message'], 'Data check failed: app-redis')
        self.assertEqual(report['data_checks'][0]['state'], 'failed after repair')

    def test_no_data_repair_without_a_verified_backup(self):
        self.clean_shutdown()
        self.host.aof_valid = False
        self.host.disk_ok = False
        self.assertEqual(self.guard(), 1)
        report = self.report()
        self.assertIn('refusing to repair without a backup', report['data_checks'][0]['state'])
        self.assertEqual(self.redis_runs('--fix'), [])

    def test_abrupt_reset_takes_one_backup_for_storage_and_data(self):
        self.host.aof_valid = False
        self.assertEqual(self.guard(), 0)
        self.assertEqual(self.report()['state'], 'data_repaired')
        tars = [c for c in self.host.calls if c[:3] == ['podman', 'unshare', 'tar']]
        self.assertEqual(len(tars), 1)

    def test_running_redis_is_not_checked(self):
        self.clean_shutdown()
        self.host.aof_valid = False
        self.host.running = {'app-redis.service'}
        self.assertEqual(self.guard(), 0)
        report = self.report()
        self.assertEqual(report['state'], 'clean')
        self.assertEqual(report['data_checks'][0]['state'], 'skipped: app-redis is running')
        self.assertEqual(self.redis_runs(), [])

    def test_rdb_only_folder_is_checked_and_reported(self):
        shutil.rmtree(self.redis / 'appendonlydir')
        (self.redis / 'dump.rdb').write_bytes(b'REDIS0012')
        self.clean_shutdown()
        self.host.rdb_valid = False
        self.assertEqual(self.guard(), 1)
        result = self.report()['data_checks'][0]
        self.assertEqual(result['state'], 'failed (no repair configured)')
        self.assertEqual(self.redis_runs()[0][-1], '/data/dump.rdb')
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_empty_redis_folder_is_fine(self):
        shutil.rmtree(self.redis / 'appendonlydir')
        self.clean_shutdown()
        self.assertEqual(self.guard(), 0)
        self.assertEqual(self.report()['data_checks'][0]['state'], 'no data')

    def test_generic_command_check_and_repair(self):
        self.settings['data_checks'] = [{'kind': 'command', 'name': 'app-check',
                                         'check': ['my-check'], 'repair': ['my-repair']}]
        self.clean_shutdown()
        self.host.command_ok = False
        self.assertEqual(self.guard(), 0)
        self.assertEqual(self.report()['data_checks'][0]['state'], 'repaired')

    def test_misconfigured_check_is_reported_not_fatal(self):
        self.settings['data_checks'] = [{'kind': 'redis', 'unit': 'app-missing', 'path': str(self.redis)},
                                        {'kind': 'nonsense'}]
        self.clean_shutdown()
        self.assertEqual(self.guard(), 1)
        states = [r['state'] for r in self.report()['data_checks']]
        self.assertTrue(states[0].startswith('error: app-missing: no image pinned'), states)
        self.assertTrue(states[1].startswith('error: unknown data check kind'), states)

    def test_data_checks_wait_for_usable_storage(self):
        self.clean_shutdown()
        self.host.check_fails = True
        self.host.disk_ok = False
        self.assertEqual(self.guard(), 1)
        self.assertNotIn('data_checks', self.report())
        self.assertEqual(self.redis_runs(), [])

    def test_check_action_never_repairs(self):
        self.host.aof_valid = False
        with patch.object(guard, 'load_settings', return_value=self.settings), \
             patch('builtins.print') as printed:
            self.assertEqual(guard.main(['check']), 1)
        output = json.loads(printed.call_args.args[0])
        self.assertEqual(output['data_checks'][0]['state'], 'failed (check only)')
        self.assertEqual(self.redis_runs('--fix'), [])
        self.assertEqual(list(self.backups.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
