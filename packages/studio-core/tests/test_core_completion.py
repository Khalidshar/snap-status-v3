"""Shared core acceptance tests: real SQLite/WAL, no external transport."""
import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from studio_core.observability.agent_inventory import APPLICATIONS, ROLE_IDS, inventory
from studio_core.storage.backup import create_snapshot, probe_restore, main
from studio_core.config import Config
from studio_core.jobs import Repository

ROOT = Path(__file__).parents[1]
REVISION = 'a' * 40


def registry():
    return {'agents': [{'id': role, 'applications': list(APPLICATIONS),
                        'state_isolated_per_application': True,
                        'execution_policy': 'WHEN_APPLICABLE',
                        'acceptance_status': 'VERIFIED',
                        'error': 'private-example-input'} for role in ROLE_IDS]}


class InventoryTests(unittest.TestCase):
    def test_two_apps_have_thirty_six_distinct_unaccepted_identities(self):
        snap = inventory(registry(), APPLICATIONS[0], REVISION, {
            'fact_source': {'scope': 'weather', 'kind': 'OBSERVER'}})
        status = inventory(registry(), APPLICATIONS[1], REVISION, {})
        rows = snap['agents'] + status['agents']
        self.assertEqual(len(rows), 36)
        self.assertEqual(len({row['instance_id'] for row in rows}), 36)
        self.assertTrue(all(row['function_acceptance'] == 'NOT_VERIFIED' for row in rows))
        self.assertTrue(all(row['runtime_state'] == 'UNKNOWN' for row in rows))
        self.assertFalse(snap['production_ready']); self.assertFalse(status['delivery_authority'])
        self.assertEqual(next(row for row in status['agents'] if row['role_id'] == 'fact_source')['binding'], 'NOT_BOUND')
        self.assertNotIn('private-example-input', json.dumps(rows))

    def test_inputs_are_not_mutated_and_output_hash_is_reproducible(self):
        source = registry(); original = copy.deepcopy(source)
        bindings = {'arabic': {'scope': 'did_you_know', 'kind': 'LOCAL_RULE_ADAPTER'}}
        result = inventory(source, APPLICATIONS[0], REVISION, bindings)
        self.assertEqual(result, inventory(source, APPLICATIONS[0], REVISION, bindings))
        self.assertEqual(source, original)
        digest = result.pop('sha256')
        self.assertEqual(digest, hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest())

    def test_missing_duplicate_renamed_and_cross_app_roles_fail(self):
        variants = []
        value = registry(); value['agents'].pop(); variants.append(value)
        value = registry(); value['agents'][0]['id'] = ROLE_IDS[1]; variants.append(value)
        value = registry(); value['agents'][0]['id'] = 'new_role'; variants.append(value)
        value = registry(); value['agents'][0]['applications'] = [APPLICATIONS[0]]; variants.append(value)
        value = registry(); value['agents'][0]['state_isolated_per_application'] = False; variants.append(value)
        value = registry(); value['agents'][0]['execution_policy'] = 'ALWAYS'; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                inventory(value, APPLICATIONS[0], REVISION, {})

    def test_forged_binding_acceptance_and_unknown_fields_fail(self):
        for bindings in ({'new_role': {}}, {'voice': {'scope': 'weather', 'kind': 'AI'}},
                         {'voice': {'scope': 'weather', 'kind': 'OBSERVER', 'accepted': True}},
                         {'voice': {'scope': 'private/token', 'kind': 'OBSERVER'}},
                         {'voice': {'scope': None, 'kind': 'OBSERVER'}}):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                inventory(registry(), APPLICATIONS[0], REVISION, bindings)

    def test_bad_identity_revision_and_registry_fail(self):
        for app, revision, source in (('legacy', REVISION, registry()),
                                      (APPLICATIONS[0], 'main', registry()),
                                      (APPLICATIONS[0], REVISION, {}),
                                      (APPLICATIONS[0], REVISION, None)):
            with self.subTest(app=app), self.assertRaises(ValueError):
                inventory(source, app, revision, {})


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = replace(Config.load(Path(__file__).parent / 'fixtures/foundation.toml'),
                              data_root=self.root / 'state')
        self.repo = Repository(self.config, clock=lambda: 100)
        self.job, _ = self.repo.enqueue('fixture', 'fixture-actor', 'demo', 'NORMAL', 'input-digest', 1000)

    def snapshot(self, **options):
        return create_snapshot(self.repo.path, self.root / 'backups', self.config.app,
                               REVISION, now=100, **options)

    def probe(self, snapshot):
        return probe_restore(snapshot, self.config.app, self.root / 'restore-scratch')

    def test_real_committed_wal_restore_preserves_state_and_never_resumes_jobs(self):
        with self.repo.connect() as active:
            active.execute('BEGIN IMMEDIATE')
            active.execute('INSERT INTO events(job_id,at,event,details) VALUES(?,?,?,?)',
                           (self.job['id'], 101, 'fixture.committed', '{}'))
            active.commit()
            self.assertTrue(Path(str(self.repo.path) + '-wal').exists())
            before = self.repo.timeline(self.job['id'])
            snapshot = self.snapshot()
            result = self.probe(snapshot)
        self.assertEqual(result['result'], 'PASS')
        self.assertEqual(result['counts']['events'], len(before))
        self.assertEqual(result['counts']['jobs'], 1)
        self.assertFalse(result['jobs_resumed']); self.assertFalse(result['live_state_modified'])
        self.assertEqual(before, self.repo.timeline(self.job['id']))
        self.assertEqual(list((self.root / 'restore-scratch').iterdir()), [])
        self.assertNotIn(self.job['id'], json.dumps(result))

    def test_pending_delivery_state_and_lease_are_retained_without_replay(self):
        claim = self.repo.claim(job_id=self.job['id'])
        with self.repo.transaction() as db:
            db.execute("INSERT INTO deliveries VALUES(?,?,'UNKNOWN')", ('fixture-unknown', self.job['id']))
        before = self.repo.get(self.job['id'])
        snapshot = self.snapshot(); result = self.probe(snapshot)
        self.assertEqual(result['counts']['deliveries'], 1)
        self.assertEqual(self.repo.get(self.job['id']), before)
        with sqlite3.connect(snapshot / 'jobs.sqlite') as db:
            self.assertEqual(db.execute('SELECT lease_token FROM jobs').fetchone()[0], claim['lease_token'])
            self.assertEqual(db.execute('SELECT state FROM deliveries').fetchone()[0], 'UNKNOWN')

    def test_private_modes_and_unique_snapshots(self):
        first, second = self.snapshot(), self.snapshot()
        self.assertNotEqual(first, second)
        self.assertEqual(first.stat().st_mode & 0o777, 0o700)
        for name in ('jobs.sqlite', 'manifest.json'):
            self.assertEqual((first / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.probe(first)['result'], 'PASS')

    def test_unknown_app_and_revision_rejected_before_output(self):
        for app, revision in (('legacy', REVISION), (self.config.app, 'main')):
            with self.assertRaises(ValueError):
                create_snapshot(self.repo.path, self.root / 'invalid', app, revision)
        self.assertFalse((self.root / 'invalid').exists())

    def test_snapshot_timeout_cleans_incomplete_bundle(self):
        with patch('studio_core.storage.backup.time.monotonic', side_effect=[0, 100]):
            with self.assertRaises(TimeoutError): self.snapshot(timeout_seconds=1)
        self.assertEqual(list((self.root / 'backups').iterdir()), [])

    def test_invalid_times_and_budgets_rejected(self):
        for timeout in (0, -1, True, float('nan'), 301):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.snapshot(timeout_seconds=timeout)
        for now in (0, True, float('inf')):
            with self.subTest(now=now), self.assertRaises(ValueError):
                create_snapshot(self.repo.path, self.root / 'backups', self.config.app, REVISION, now=now)

    def test_missing_source_does_not_create_database(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(ValueError):
            create_snapshot(missing, self.root / 'backups', self.config.app, REVISION)
        self.assertFalse(missing.exists())

    def test_cross_app_backup_and_probe_are_rejected(self):
        other = next(app for app in APPLICATIONS if app != self.config.app)
        with self.assertRaises(ValueError):
            create_snapshot(self.repo.path, self.root / 'backups', other, REVISION)
        self.assertEqual(list((self.root / 'backups').iterdir()), [])
        snapshot = self.snapshot()
        with self.assertRaises(ValueError): probe_restore(snapshot, other, self.root / 'restore-scratch')

    def test_corrupt_snapshot_rejected_and_probe_directory_cleaned(self):
        snapshot = self.snapshot()
        path = snapshot / 'jobs.sqlite'; path.write_bytes(path.read_bytes() + b'changed')
        with self.assertRaises(ValueError): self.probe(snapshot)
        self.assertEqual(list((self.root / 'restore-scratch').iterdir()), [])

    def test_manifest_schema_paths_hash_counts_and_extra_fields_rejected(self):
        changes = ({'file': '../live.sqlite'}, {'schema': True}, {'counts': {}},
                   {'app': 'legacy'}, {'created_at': float('nan')},
                   {'revision': 'main'}, {'sha256': 'invalid'}, {'secret': 'private-input'},
                   {'counts': {'jobs': True, 'commands': 1, 'events': 1, 'deliveries': 0}})
        for change in changes:
            with self.subTest(change=change):
                snapshot = self.snapshot(); path = snapshot / 'manifest.json'
                value = json.loads(path.read_text()); value.update(change); path.write_text(json.dumps(value))
                with self.assertRaises(ValueError): self.probe(snapshot)

    def test_symlink_snapshot_source_parent_and_file_are_rejected(self):
        link = self.root / 'source-link'; link.symlink_to(self.repo.path)
        with self.assertRaises(ValueError):
            create_snapshot(link, self.root / 'backups', self.config.app, REVISION)
        parent = self.root / 'parent-link'; parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            create_snapshot(self.repo.path, parent / 'backups', self.config.app, REVISION)
        snapshot = self.snapshot(); database = snapshot / 'jobs.sqlite'
        database.unlink(); database.symlink_to(self.repo.path)
        with self.assertRaises(ValueError): self.probe(snapshot)

    def test_world_readable_directory_files_and_sidecars_are_rejected(self):
        unsafe = self.root / 'public'; unsafe.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            create_snapshot(self.repo.path, unsafe, self.config.app, REVISION)
        for mutation in ('mode', 'sidecar', 'wal-symlink'):
            with self.subTest(mutation=mutation):
                snapshot = self.snapshot()
                if mutation == 'mode': (snapshot / 'jobs.sqlite').chmod(0o644)
                elif mutation == 'sidecar': (snapshot / 'jobs.sqlite-wal').write_bytes(b'')
                else: (snapshot / 'jobs.sqlite-wal').symlink_to(self.root / 'missing')
                with self.assertRaises(ValueError): self.probe(snapshot)

    def test_cli_uses_config_app_and_does_not_export_private_errors(self):
        output = io.StringIO()
        with patch('studio_core.config.Config.load', return_value=self.config), redirect_stdout(output):
            self.assertEqual(main(['snapshot', '--config', 'fixture-config',
                                   '--backup-root', str(self.root / 'backups'),
                                   '--revision', REVISION]), 0)
        created = json.loads(output.getvalue())
        self.assertEqual(created['app'], self.config.app)
        output = io.StringIO()
        with patch('studio_core.config.Config.load', return_value=self.config), redirect_stdout(output):
            self.assertEqual(main(['probe', '--config', 'fixture-config',
                                   '--snapshot', created['snapshot'],
                                   '--scratch-root', str(self.root / 'restore-scratch')]), 0)
        self.assertEqual(json.loads(output.getvalue())['result'], 'PASS')
        output = io.StringIO()
        with patch('studio_core.config.Config.load', side_effect=OSError('private-value')), redirect_stdout(output):
            self.assertEqual(main(['probe', '--config', 'fixture-config']), 1)
        self.assertNotIn('private-value', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['code'], 'BACKUP_OR_PROBE_REJECTED')


if __name__ == '__main__':
    unittest.main()
