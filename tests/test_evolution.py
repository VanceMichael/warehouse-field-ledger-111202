"""Tests for schema evolution, field identity and versioned projection.

Projection is reopened (and, in the restart test, re-driven from a separate
process) between phases: it must depend only on persisted registrations, never
on an in-memory identity map.
"""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from warehouse.catalog import connect, load_bundle, prepare_file, register, write_bundle
from warehouse.domain import overview, rows_at, version_view
from warehouse.registry import Registry, RegistryConflict
from warehouse.schema import (
    SchemaError,
    add_field,
    compare,
    drop_field,
    rename_field,
    set_field_type,
    table_map,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'examples/source.json'
EXPECTED = json.loads((SOURCE.parent / 'expected.json').read_text(encoding='utf-8'))


def source_bundle():
    return json.loads(SOURCE.read_text(encoding='utf-8'))


class WarehouseTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'warehouse.sqlite3'
        self.connection = connect(self.path)
        self.registry = Registry(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def reopen(self):
        self.connection.close()
        self.connection = connect(self.path)
        self.registry = Registry(self.connection)
        return self.connection

    def register(self, definition, actor='tester', note='', base_version=None):
        return register(self.connection, definition, actor=actor, note=note, base_version=base_version)

    def current(self):
        return self.registry.current()


class IdentityTests(WarehouseTestCase):
    def test_baseline_persists_field_ids(self):
        first = self.registry.identities()
        self.assertEqual(len(first), 4)
        self.reopen()
        self.assertEqual([row['field_id'] for row in self.registry.identities()],
                         [row['field_id'] for row in first])

    def test_rename_keeps_field_identity(self):
        definition = self.current()['definition']
        temperature = table_map(definition['measurements'])[
            (definition['measurements']['fields'][2]['field_id'],)
        ]
        record = self.register(rename_field(definition, 'measurements', ('temperature',), 'reading'))
        renamed = record['definition']['measurements']['fields'][2]
        self.assertEqual(renamed['name'], 'reading')
        self.assertEqual(renamed['field_id'], temperature['field_id'])

    def test_old_file_projects_across_rename_after_reopen(self):
        load_bundle(self.connection, source_bundle(), 'first')
        self.register(rename_field(self.current()['definition'],
                                   'measurements', ('temperature',), 'reading'))
        expected = [
            {'sample': 'm1', 'device': 'd1', 'reading': '22.50', 'unit': 'C'},
            {'sample': 'm2', 'device': 'd1', 'reading': None, 'unit': 'C'},
        ]
        self.assertEqual(overview(self.connection), expected)
        self.reopen()
        self.assertEqual(overview(self.connection), expected)

    def test_drop_then_reuse_name_yields_new_identity(self):
        load_bundle(self.connection, source_bundle(), 'first')
        v2 = self.register(rename_field(self.current()['definition'],
                                        'measurements', ('temperature',), 'reading'))
        v3 = self.register(drop_field(v2['definition'], 'measurements', ('reading',)))
        self.assertEqual(len(self.registry.retired_field_ids()), 1)
        v4 = self.register(add_field(v3['definition'], 'measurements', (), 'reading', 'string', True))
        old_id = v2['definition']['measurements']['fields'][2]['field_id']
        new_id = v4['definition']['measurements']['fields'][-1]['field_id']
        self.assertNotEqual(old_id, new_id)
        self.assertNotIn(new_id, self.registry.retired_field_ids())
        # Old measurements must not be reinterpreted as the new string field.
        self.assertIsNone(rows_at(self.connection, 4)[0]['reading'])
        kinds = {row['kind'] for row in self.registry.audit()[-1]['detail']['operations']}
        self.assertEqual(kinds, {'add_nullable', 'reuse_name'})

    def test_retired_identity_cannot_be_reused(self):
        v2 = self.register(rename_field(self.current()['definition'],
                                        'measurements', ('temperature',), 'reading'))
        v3 = self.register(drop_field(v2['definition'], 'measurements', ('reading',)))
        retired_id = v2['definition']['measurements']['fields'][2]['field_id']
        broken = copy.deepcopy(v3['definition'])
        broken['measurements']['fields'].append({
            'name': 'again', 'field_id': retired_id, 'type': 'string',
            'nullable': True, 'nested': None,
        })
        with self.assertRaises(RegistryConflict) as raised:
            self.register(broken)
        self.assertEqual(raised.exception.code, 'incompatible_change')
        self.assertEqual(raised.exception.detail['blockers'][0]['field_id'], retired_id)
        self.assertEqual(self.current()['version'], 3)

    def test_nested_rename_preserves_identity(self):
        v2 = self.register(_with_instrument(self.current()['definition']))
        load_bundle(self.connection, {'measurements': [{
            'sample': 's1', 'device': 'd1', 'unit': 'C', 'temperature': '1',
            'instrument': {'serial': 7},
        }]}, 'nested', writer_schema_id=v2['schema_id'])
        renamed = rename_field(v2['definition'], 'measurements', ('instrument', 'serial'), 'number')
        renamed = rename_field(renamed, 'measurements', ('instrument',), 'meter')
        v3 = self.register(renamed)
        self.assertEqual(rows_at(self.connection, 3),
                         [{'sample': 's1', 'device': 'd1', 'temperature': '1',
                           'unit': 'C', 'meter': {'number': 7}}])
        self.reopen()
        self.assertEqual(rows_at(self.connection, 3)[0]['meter'], {'number': 7})

    def test_nested_name_reuse_gets_new_identity(self):
        v2 = self.register(_with_instrument(self.current()['definition']))
        load_bundle(self.connection, {'measurements': [{
            'sample': 's1', 'device': 'd1', 'unit': 'C', 'temperature': '1',
            'instrument': {'serial': 7},
        }]}, 'nested', writer_schema_id=v2['schema_id'])
        v3 = self.register(drop_field(v2['definition'], 'measurements', ('instrument',)))
        v4 = self.register(_with_instrument(v3['definition'], serial_type='string'))
        self.assertIsNone(rows_at(self.connection, 4)[0]['instrument'])


class CompatibilityTests(WarehouseTestCase):
    def test_add_nullable_is_compatible(self):
        self.assertEqual(self.register(
            add_field(self.current()['definition'], 'measurements', (), 'note', 'string', True)
        )['version'], 2)

    def test_add_required_without_default_is_blocked(self):
        with self.assertRaises(RegistryConflict) as raised:
            self.register(add_field(self.current()['definition'], 'measurements',
                                    (), 'note', 'string', False))
        self.assertEqual(raised.exception.code, 'incompatible_change')
        self.assertIn('add_required', {item['kind'] for item in raised.exception.detail['blockers']})

    def test_required_to_nullable_is_compatible(self):
        definition = copy.deepcopy(self.current()['definition'])
        definition['measurements']['fields'][3]['nullable'] = True
        self.assertEqual(self.register(definition)['version'], 2)

    def test_nullable_to_required_is_blocked(self):
        definition = copy.deepcopy(self.current()['definition'])
        definition['measurements']['fields'][2]['nullable'] = False
        with self.assertRaises(RegistryConflict) as raised:
            self.register(definition)
        self.assertIn('nullable_to_required',
                      {item['kind'] for item in raised.exception.detail['blockers']})

    def test_narrowing_is_blocked(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'count', 'integer', False, default=0))
        narrow = set_field_type(v2['definition'], 'measurements', ('count',), 'string')
        with self.assertRaises(RegistryConflict) as raised:
            self.register(narrow)
        self.assertIn('narrow', {item['kind'] for item in raised.exception.detail['blockers']})

    def test_integer_to_decimal_is_compatible(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'count', 'integer', False, default=0))
        self.assertEqual(self.register(set_field_type(
            v2['definition'], 'measurements', ('count',), 'decimal'))['version'], 3)

    def test_decimal_text_is_exact(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'count', 'integer', False, default=0))
        v3 = self.register(set_field_type(v2['definition'], 'measurements', ('count',), 'decimal'))
        load_bundle(self.connection, {'measurements': [
            {'sample': 'a', 'device': 'd1', 'unit': 'C', 'count': 0.1},
        ]}, 'decimal', writer_schema_id=v3['schema_id'])
        self.assertEqual(rows_at(self.connection, 3)[0]['count'], '0.1')

    def test_old_and_new_writers_share_one_projection(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'count', 'integer', False, default=0))
        load_bundle(self.connection, {'measurements': [{
            'sample': 'a', 'device': 'd1', 'unit': 'C', 'temperature': '1', 'count': 5,
        }]}, 'new-writer', writer_schema_id=v2['schema_id'])
        v3 = self.register(set_field_type(v2['definition'], 'measurements', ('count',), 'decimal'))
        load_bundle(self.connection, {'measurements': [{
            'sample': 'b', 'device': 'd1', 'unit': 'C', 'temperature': '2',
        }]}, 'old-writer', writer_version=1)
        rows = {row['sample']: row for row in rows_at(self.connection, 3)}
        self.assertEqual(rows['a']['count'], '5')
        self.assertEqual(rows['b']['count'], '0')

    def test_reverting_shape_is_a_new_version(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'note', 'string', True))
        v3 = self.register(drop_field(v2['definition'], 'measurements', ('note',)))
        self.assertEqual(v3['version'], 3)
        from warehouse.definition import initial_definition
        self.assertEqual(compare(initial_definition(), v3['definition'])[0], [])

    def test_stale_base_is_reported_stably(self):
        self.register(add_field(self.current()['definition'], 'measurements',
                                (), 'note', 'string', True))
        with self.assertRaises(RegistryConflict) as raised:
            self.register(add_field(self.current()['definition'], 'measurements',
                                    (), 'x', 'string', True), base_version=1)
        self.assertEqual(raised.exception.code, 'stale_base')
        self.assertEqual(raised.exception.detail['current_version'], 2)

    def test_field_relocation_is_blocked(self):
        v2 = self.register(_with_instrument(self.current()['definition'], serial_type='string',
                                              serial_nullable=True))
        moved = copy.deepcopy(v2['definition'])
        nested = moved['measurements']['fields'][-1]['nested']
        segment = nested['fields'].pop(0)
        moved['measurements']['fields'].append(segment)
        with self.assertRaises(RegistryConflict) as raised:
            self.register(moved)
        self.assertIn('relocated', {item['kind'] for item in raised.exception.detail['blockers']})

    def test_unknown_table_in_bundle_is_rejected(self):
        with self.assertRaises(ValueError):
            load_bundle(self.connection, {'unknown_table': []}, 'x')


class ProjectionTests(WarehouseTestCase):
    def test_missing_null_and_default_are_distinct(self):
        self.register(add_field(self.current()['definition'], 'measurements',
                                (), 'reading', 'string', True, default='pending'))
        load_bundle(self.connection, {'measurements': [
            {'sample': 'a', 'device': 'd1', 'unit': 'C', 'temperature': None},
            {'sample': 'b', 'device': 'd1', 'unit': 'C'},
        ]}, 'mixed', writer_version=1)
        rows = {row['sample']: row for row in overview(self.connection)}
        self.assertIsNone(rows['a']['temperature'])          # explicit null stays null
        self.assertEqual(rows['a']['reading'], 'pending')    # missing gets the default
        self.assertEqual(rows['b']['reading'], 'pending')

    def test_default_is_frozen_at_version_publication(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'reading', 'string', True, default='old-default'))
        load_bundle(self.connection, {'measurements': [
            {'sample': 'a', 'device': 'd1', 'unit': 'C'},
        ]}, 'frozen', writer_version=1)
        v3 = self.register(add_field(v2['definition'], 'measurements', (), 'note',
                                     'string', True, default='new-default'))
        self.assertEqual(rows_at(self.connection, 3)[0]['reading'], 'old-default')
        self.assertEqual(rows_at(self.connection, 3)[0]['note'], 'new-default')
        self.assertEqual(rows_at(self.connection, 2)[0]['reading'], 'old-default')

    def test_projection_uses_registered_defaults_after_reopen(self):
        self.register(add_field(self.current()['definition'], 'measurements',
                                (), 'reading', 'string', True, default='pending'))
        load_bundle(self.connection, source_bundle(), 'first', writer_version=1)
        self.reopen()
        self.assertEqual([row['reading'] for row in overview(self.connection)],
                         ['pending', 'pending'])

    def test_projection_of_missing_required_fails_loudly(self):
        # A required field with no default cannot be introduced through the API;
        # simulate a corrupt registry row to prove reads fail rather than guess.
        load_bundle(self.connection, source_bundle(), 'first')
        definition = add_field(self.current()['definition'], 'measurements',
                               (), 'required', 'string', False, default='x')
        record = self.register(definition)
        stored = json.loads(self.connection.execute(
            'SELECT definition FROM schema_versions WHERE version=?', (record['version'],)
        ).fetchone()[0])
        stored['measurements']['fields'][-1].pop('default')
        self.connection.execute('UPDATE schema_versions SET definition=? WHERE version=?',
                                (json.dumps(stored), record['version']))
        with self.assertRaises(SchemaError):
            rows_at(self.connection, record['version'])

    def test_version_view_reports_writer_and_sources(self):
        load_bundle(self.connection, source_bundle(), 'first')
        self.register(rename_field(self.current()['definition'],
                                   'measurements', ('temperature',), 'reading'))
        view = version_view(self.connection)
        self.assertEqual(view['version'], 2)
        batch = view['batches'][0]
        self.assertEqual(batch['written_version'], 1)
        self.assertEqual({item['source'] for item in batch['sources']}, {'carried'})
        self.assertIn('rename', {item['kind'] for item in view['operations']})

    def test_legacy_sql_table_follows_current_version(self):
        load_bundle(self.connection, source_bundle(), 'first')
        self.register(rename_field(self.current()['definition'],
                                   'measurements', ('temperature',), 'reading'))
        columns = [row['name'] for row in self.connection.execute('PRAGMA table_info(measurements)')]
        self.assertEqual(columns, ['sample', 'device', 'reading', 'unit'])
        self.reopen()
        rows = [dict(row) for row in
                self.connection.execute('SELECT * FROM measurements ORDER BY sample')]
        self.assertEqual(rows, [
            {'sample': 'm1', 'device': 'd1', 'reading': '22.50', 'unit': 'C'},
            {'sample': 'm2', 'device': 'd1', 'reading': None, 'unit': 'C'},
        ])


class ProducerTests(WarehouseTestCase):
    def test_token_is_rechecked_at_commit(self):
        token = prepare_file(self.connection, source_bundle(), 'second', writer_version=1)
        self.register(rename_field(self.current()['definition'],
                                   'measurements', ('temperature',), 'reading'))
        result = write_bundle(self.connection, source_bundle(), 'second', writer_token=token)
        self.assertTrue(result['changed'])
        self.assertEqual(result['version'], 1)

    def test_writer_of_dropped_field_is_rejected_and_invisible(self):
        token = prepare_file(self.connection, source_bundle(), 'stale', writer_version=1)
        v2 = self.register(rename_field(self.current()['definition'],
                                        'measurements', ('temperature',), 'reading'))
        self.register(drop_field(v2['definition'], 'measurements', ('reading',)))
        with self.assertRaises(RegistryConflict) as raised:
            write_bundle(self.connection, source_bundle(), 'stale', writer_token=token)
        self.assertEqual(raised.exception.code, 'incompatible_writer_version')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM ingest_batches').fetchone()[0], 0)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM stored_rows').fetchone()[0], 0)
        self.reopen()
        self.assertEqual(overview(self.connection), [])
        audit = [tuple(row) for row in self.connection.execute(
            'SELECT status, reason FROM ingest_batches_audit ORDER BY attempt')]
        self.assertIn(('rejected', 'incompatible_writer_version'), audit)

    def test_unknown_writer_version_is_audited(self):
        with self.assertRaises(RegistryConflict) as raised:
            load_bundle(self.connection, source_bundle(), 'future', writer_version=99)
        self.assertEqual(raised.exception.code, 'unknown_version')
        row = self.connection.execute(
            'SELECT status, reason FROM ingest_batches_audit ORDER BY attempt DESC LIMIT 1').fetchone()
        self.assertEqual((row['status'], row['reason']), ('rejected', 'unknown_version'))

    def test_batch_digest_uses_writer_version(self):
        load_bundle(self.connection, source_bundle(), 'daily')
        self.register(rename_field(self.current()['definition'],
                                   'measurements', ('temperature',), 'reading'))
        self.assertFalse(load_bundle(self.connection, source_bundle(), 'daily', writer_version=1))

    def test_conflicting_batch_still_errors(self):
        load_bundle(self.connection, source_bundle(), 'daily')
        with self.assertRaises(ValueError):
            load_bundle(self.connection, {'measurements': []}, 'daily')


class AuditTests(WarehouseTestCase):
    def test_registration_audit_has_signature(self):
        record = self.register(add_field(self.current()['definition'], 'measurements',
                                         (), 'note', 'string', True), actor='alice', note='add note')
        entry = self.registry.audit()[-1]
        self.assertEqual((entry['kind'], entry['actor'], entry['version']),
                         ('registered', 'alice', 2))
        self.assertEqual(entry['detail']['schema_id'], record['schema_id'])
        self.assertIn('signature', entry['detail'])

    def test_rejection_is_audited_and_current_unchanged(self):
        v2 = self.register(add_field(self.current()['definition'], 'measurements',
                                     (), 'note', 'string', True))
        narrowed = copy.deepcopy(v2['definition'])
        narrowed['measurements']['fields'][2]['nullable'] = False  # temperature
        with self.assertRaises(RegistryConflict):
            self.register(narrowed)
        self.assertEqual(self.current()['version'], 2)
        rejected = [entry for entry in self.registry.audit() if entry['kind'] == 'rejected']
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]['detail']['code'], 'incompatible_change')
        self.assertIn('signature', rejected[0]['detail'])

    def test_loser_of_concurrent_registration_is_audited(self):
        base = self.current()['definition']
        first = add_field(base, 'measurements', (), 'alpha', 'string', True)
        second = add_field(base, 'measurements', (), 'beta', 'string', True)
        other = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        other.row_factory = sqlite3.Row
        try:
            register(self.connection, first, actor='a')
            with self.assertRaises(RegistryConflict) as raised:
                register(other, second, actor='b', base_version=1)
        finally:
            other.close()
        self.assertEqual(raised.exception.code, 'stale_base')
        losers = [entry for entry in self.registry.audit() if entry['actor'] == 'b']
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0]['kind'], 'rejected')


class LegacyUpgradeTests(unittest.TestCase):
    """A database written before the registry must upgrade, not guess."""

    def test_flat_legacy_database_is_adopted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'legacy.sqlite3'
            legacy = sqlite3.connect(database)
            legacy.execute(
                'CREATE TABLE measurements (sample TEXT PRIMARY KEY, device TEXT NOT NULL, '
                'temperature TEXT, unit TEXT NOT NULL)'
            )
            legacy.execute('INSERT INTO measurements VALUES (?,?,?,?)', ('m1', 'd1', '22.50', 'C'))
            legacy.execute('INSERT INTO measurements VALUES (?,?,?,?)', ('m2', 'd1', None, 'C'))
            legacy.commit()
            legacy.close()
            connection = connect(database)
            try:
                self.assertEqual(overview(connection), [
                    {'sample': 'm1', 'device': 'd1', 'temperature': '22.50', 'unit': 'C'},
                    {'sample': 'm2', 'device': 'd1', 'temperature': None, 'unit': 'C'},
                ])
                self.assertEqual([row['batch'] for row in
                                  connection.execute('SELECT batch FROM ingest_batches')], ['legacy'])
            finally:
                connection.close()
            # Reopening must not adopt the (now derived) flat table again.
            connection = connect(database)
            try:
                self.assertEqual(len(overview(connection)), 2)
                self.assertEqual([row['batch'] for row in
                                  connection.execute('SELECT batch FROM ingest_batches')], ['legacy'])
            finally:
                connection.close()

    def test_legacy_temperature_does_not_fill_reused_note_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'legacy.sqlite3'
            legacy = sqlite3.connect(database)
            legacy.execute(
                'CREATE TABLE measurements (sample TEXT PRIMARY KEY, device TEXT NOT NULL, '
                'temperature TEXT, unit TEXT NOT NULL)'
            )
            legacy.execute('INSERT INTO measurements VALUES (?,?,?,?)', ('m1', 'd1', '22.50', 'C'))
            legacy.commit()
            legacy.close()
            connection = connect(database)
            try:
                registry = Registry(connection)
                v2 = register(connection, rename_field(
                    registry.current()['definition'], 'measurements', ('temperature',), 'reading'
                ), actor='a')
                v3 = register(connection, drop_field(
                    v2['definition'], 'measurements', ('reading',)
                ), actor='a')
                v4 = register(connection, add_field(
                    v3['definition'], 'measurements', (), 'temperature', 'string', True
                ), actor='a')
                row = rows_at(connection, v4['version'])[0]
                self.assertEqual(row['temperature'], None)
                self.assertNotIn('reading', row)
            finally:
                connection.close()

    def test_unknown_legacy_column_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'legacy.sqlite3'
            legacy = sqlite3.connect(database)
            legacy.execute('CREATE TABLE measurements (sample TEXT, mystery TEXT)')
            legacy.commit()
            legacy.close()
            with self.assertRaises(RegistryConflict) as raised:
                connect(database)
            self.assertEqual(raised.exception.code, 'legacy_table_mismatch')


class RestartTests(unittest.TestCase):
    """Historical files must project from a cold process, not just memory."""

    def test_projection_after_process_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / 'warehouse.sqlite3'
            environment = dict(os.environ, PYTHONPATH=str(ROOT))

            prepare = subprocess.run(
                [sys.executable, '-m', 'tests.fixtures.projection_probe', 'prepare', str(database)],
                text=True, capture_output=True, env=environment, cwd=ROOT,
            )
            self.assertEqual(prepare.returncode, 0, prepare.stderr)
            self.assertEqual(json.loads(prepare.stdout)['version'], 2)

            probe = subprocess.run(
                [sys.executable, '-m', 'tests.fixtures.projection_probe', 'read', str(database)],
                text=True, capture_output=True, env=environment, cwd=ROOT,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(json.loads(probe.stdout), [
                {'sample': 'm1', 'device': 'd1', 'reading': '22.50', 'unit': 'C'},
                {'sample': 'm2', 'device': 'd1', 'reading': None, 'unit': 'C'},
            ])


def _with_instrument(definition, serial_type='integer', serial_nullable=False):
    result = add_field(definition, 'measurements', (), 'instrument', 'object', True)
    return add_field(result, 'measurements', ('instrument',), 'serial', serial_type,
                     serial_nullable)
