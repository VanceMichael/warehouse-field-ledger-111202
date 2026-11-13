"""Persistent schema registry: frozen versions, field identities and audit.

Registration is the only way a definition becomes authoritative.  It is a
serialised SQLite transaction (``BEGIN IMMEDIATE``), so concurrent attempts
produce one winner and deterministic, auditable conflicts for everyone else.
Field identities are recorded separately from versions: an identity that is
retired when a field is dropped can never be revived, while a reused *name*
gets a brand new identity.
"""

import json
from datetime import datetime, timezone

from .schema import (
    COMPATIBLE,
    INCOMPATIBLE,
    SchemaError,
    canonical,
    change_signature,
    compare,
    copy_definition,
    encode_json,
    summarize,
    validate_definition,
    walk_table,
)


class RegistryConflict(Exception):
    """A registration or write cannot proceed; ``code`` is stable."""

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def as_dict(self):
        return {'code': self.code, 'message': str(self), **self.detail}


class IncompatibleChange(RegistryConflict):
    def __init__(self, operations, blockers):
        super().__init__(
            'incompatible_change',
            '结构变化包含不可兼容操作: ' + summarize(operations),
            operations=[_operation_to_json(item) for item in operations],
            blockers=blockers,
            signature=change_signature(operations),
        )


def _operation_to_json(operation):
    kind, category, detail = operation
    return {'kind': kind, 'category': category, 'detail': detail}


def now_text():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Registry:
    def __init__(self, connection):
        self.connection = connection

    # -- reads -------------------------------------------------------------

    def current(self):
        row = self.connection.execute(
            'SELECT schema_id, version, definition, owner, note, created_at '
            'FROM schema_versions ORDER BY version DESC LIMIT 1'
        ).fetchone()
        if row is None:
            return None
        return _version_record(row)

    def get_version(self, version=None, schema_id=None):
        if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
            raise RegistryConflict('invalid_version', '版本号必须是整数', version=version)
        if version is None and schema_id is None:
            raise RegistryConflict('invalid_lookup', '需要版本号或结构标识')
        if version is not None:
            row = self.connection.execute(
                'SELECT schema_id, version, definition, owner, note, created_at '
                'FROM schema_versions WHERE version=?', (version,)
            ).fetchone()
        else:
            # Content identifiers may repeat when a structure is restored in a
            # later version; resolve a writer to the most recent publication.
            row = self.connection.execute(
                'SELECT schema_id, version, definition, owner, note, created_at '
                'FROM schema_versions WHERE schema_id=? ORDER BY version DESC LIMIT 1',
                (schema_id,)
            ).fetchone()
        if row is None:
            raise RegistryConflict(
                'unknown_version', '登记表中不存在该结构版本',
                version=version, schema_id=schema_id,
            )
        return _version_record(row)

    def versions(self):
        return self._version_summaries()

    def _version_summaries(self):
        rows = self.connection.execute(
            'SELECT schema_id, version, definition, owner, note, created_at '
            'FROM schema_versions ORDER BY version'
        ).fetchall()
        return [_version_record(row) for row in rows]

    def audit(self, version=None):
        if version is None:
            rows = self.connection.execute(
                'SELECT audit_id, schema_id, version, kind, actor, detail, created_at '
                'FROM schema_audit ORDER BY audit_id'
            ).fetchall()
        else:
            rows = self.connection.execute(
                'SELECT audit_id, schema_id, version, kind, actor, detail, created_at '
                'FROM schema_audit WHERE version=? ORDER BY audit_id', (version,)
            ).fetchall()
        return [
            {
                'audit_id': row['audit_id'],
                'schema_id': row['schema_id'],
                'version': row['version'],
                'kind': row['kind'],
                'actor': row['actor'],
                'detail': json.loads(row['detail']),
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def identities(self):
        rows = self.connection.execute(
            'SELECT field_id, table_name, name_path, first_version, retired_version '
            'FROM schema_field_identities ORDER BY first_version, field_id'
        ).fetchall()
        return [
            {
                'field_id': row['field_id'],
                'table': row['table_name'],
                'name_path': json.loads(row['name_path']),
                'first_version': row['first_version'],
                'retired_version': row['retired_version'],
            }
            for row in rows
        ]

    def retired_field_ids(self):
        return {
            row['field_id'] for row in self.connection.execute(
                'SELECT field_id FROM schema_field_identities WHERE retired_version IS NOT NULL'
            )
        }

    # -- bootstrap ---------------------------------------------------------

    def bootstrap(self, definition, actor='system', note='初始登记'):
        if self.current() is not None:
            return self.current()
        validate_definition(definition)
        with self._transaction():
            return self.bootstrap_in_transaction(definition, actor=actor, note=note)

    def bootstrap_in_transaction(self, definition, actor='system', note='初始登记'):
        """Bootstrap when the caller already holds the write transaction."""
        if self.current() is not None:
            return self.current()
        validate_definition(definition)
        return self._write_version(definition, version=1, actor=actor, note=note, operations=[])

    # -- registration ------------------------------------------------------

    def register(self, definition, actor, note='', base_version=None, base_schema_id=None):
        with self._transaction():
            return self.register_in_transaction(
                definition, actor=actor, note=note,
                base_version=base_version, base_schema_id=base_schema_id,
            )

    def register_in_transaction(self, definition, actor, note='', base_version=None,
                                base_schema_id=None):
        """Register while the caller already holds ``BEGIN IMMEDIATE``."""
        current = self.current()
        attempted_id = _safe_definition_id(definition)
        if not isinstance(actor, str) or not actor:
            self._reject('invalid_actor', '注册需要非空的执行者标识', actor or '?', attempted_id)
        try:
            validate_definition(definition)
        except SchemaError as error:
            self._reject('invalid_definition', str(error), actor, attempted_id)
        if current is None:
            raise RegistryConflict('uninitialised', '登记表尚未建立基线')

        base_mismatch = (
            (base_version is not None and base_version != current['version'])
            or (base_schema_id is not None and base_schema_id != current['schema_id'])
        )
        if base_mismatch:
            self._reject(
                'stale_base',
                '注册依据的版本已被其他写入者升级',
                actor,
                attempted_id,
                base_version=base_version,
                base_schema_id=base_schema_id,
                current_version=current['version'],
                current_schema_id=current['schema_id'],
            )

        if attempted_id == current['schema_id']:
            self._reject(
                'no_change',
                '结构没有变化，无需登记新版本',
                actor,
                attempted_id,
                current_version=current['version'],
                current_schema_id=current['schema_id'],
            )


        retired = self.retired_field_ids()
        reused = self._reused_identities(definition, retired)
        if reused:
            operations = [
                ('reuse_retired_identity', INCOMPATIBLE, detail) for detail in reused
            ]
            blockers = list(reused)
            self._audit(
                current['schema_id'], current['version'] + 1, 'rejected', actor,
                {'code': 'incompatible_change', 'attempted_schema_id': attempted_id,
                 'operations': [_operation_to_json(item) for item in operations],
                 'blockers': blockers, 'signature': change_signature(operations)},
            )
            raise IncompatibleChange(operations, blockers)

        operations, blockers = compare(current['definition'], definition)
        operations.extend(self._reuse_operations(current['definition'], definition))
        if blockers:
            self._audit(
                current['schema_id'], current['version'] + 1, 'rejected', actor,
                {'code': 'incompatible_change', 'attempted_schema_id': attempted_id,
                 'operations': [_operation_to_json(item) for item in operations],
                 'blockers': blockers, 'signature': change_signature(operations)},
            )
            raise IncompatibleChange(operations, blockers)

        version = current['version'] + 1
        record = self._write_version(
            definition, version=version, actor=actor, note=note,
            operations=operations, previous=current,
        )
        return record

    def _reuse_operations(self, current_definition, definition):
        """Report names that were retired earlier and now host a new identity.

        Adjacent-version comparison cannot see this after two steps; the
        persisted identity ledger can.
        """
        retired_names = {}
        for row in self.connection.execute(
            'SELECT field_id, table_name, name_path FROM schema_field_identities '
            'WHERE retired_version IS NOT NULL'
        ).fetchall():
            table, names = json.loads(row['name_path'])
            retired_names.setdefault(table, {}).setdefault('.'.join(names), row['field_id'])
        operations = []
        if not retired_names:
            return operations
        for table, table_definition in definition.items():
            for ids, names, segment in walk_table(table_definition):
                old_id = retired_names.get(table, {}).get('.'.join(names))
                if old_id is None or old_id == segment['field_id']:
                    continue
                operations.append(('reuse_name', COMPATIBLE, {
                    'table': table, 'path': '.'.join(names), 'name': segment['name'],
                    'retired_field_id': old_id, 'field_id': segment['field_id'],
                }))
        operations.sort(key=lambda item: canonical(item[2]))
        return operations

    def _reused_identities(self, definition, retired):
        """Identities that must not appear in a new version.

        Only retired identities are checked: a live identity legitimately
        moves when its field is renamed, while a retired one can never be
        attached to any field again.  Duplicate use *within* a version is
        already rejected by definition validation.
        """
        violations = []
        if not retired:
            return violations
        for table, table_definition in definition.items():
            for ids, names, segment in walk_table(table_definition):
                if segment['field_id'] in retired:
                    violations.append({
                        'table': table, 'path': '.'.join(names),
                        'field_id': segment['field_id'],
                        'reason': '退役身份不能在新的版本中重新使用',
                    })
        return violations

    def _write_version(self, definition, version, actor, note, operations, previous=None):
        frozen = copy_definition(definition)
        schema_id = _definition_id(frozen)
        created = now_text()
        self.connection.execute(
            'INSERT INTO schema_versions (schema_id, version, definition, owner, note, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (schema_id, version, encode_json(frozen).decode('utf-8'), actor, note, created),
        )
        self._refresh_identities(frozen, version)
        detail = {
            'schema_id': schema_id,
            'operations': [_operation_to_json(item) for item in operations],
            'summary': summarize(operations),
            'signature': change_signature(operations),
        }
        if previous:
            detail['previous_version'] = previous['version']
            detail['previous_schema_id'] = previous['schema_id']
        self._audit(schema_id, version, 'registered', actor, detail, created)
        return {
            'schema_id': schema_id,
            'version': version,
            'definition': frozen,
            'owner': actor,
            'note': note,
            'created_at': created,
        }

    def _refresh_identities(self, definition, version):
        existing = {
            row['field_id']: row for row in self.connection.execute(
                'SELECT field_id, table_name, name_path, first_version, retired_version '
                'FROM schema_field_identities'
            )
        }
        active_now = set()
        for table, table_definition in definition.items():
            for ids, names, segment in walk_table(table_definition):
                field_id = segment['field_id']
                active_now.add(field_id)
                path = json.dumps([table, list(names)], ensure_ascii=False)
                row = existing.get(field_id)
                if row is None:
                    self.connection.execute(
                        'INSERT INTO schema_field_identities '
                        '(field_id, table_name, name_path, first_version, retired_version) '
                        'VALUES (?, ?, ?, ?, NULL)',
                        (field_id, table, path, version),
                    )
                else:
                    # Live identities move with their field; a retired one is
                    # never revived, but cannot appear here because register()
                    # rejects retired ids before reaching this point.
                    self.connection.execute(
                        'UPDATE schema_field_identities SET name_path=?, retired_version=NULL '
                        'WHERE field_id=?',
                        (path, field_id),
                    )
        for field_id, row in existing.items():
            if field_id in active_now or row['retired_version'] is not None:
                continue
            self.connection.execute(
                'UPDATE schema_field_identities SET retired_version=? WHERE field_id=?',
                (version, field_id),
            )

    def _audit(self, schema_id, version, kind, actor, detail, created=None):
        self.connection.execute(
            'INSERT INTO schema_audit (schema_id, version, kind, actor, detail, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (schema_id, version, kind, actor, encode_json(detail).decode('utf-8'), created or now_text()),
        )

    def _reject(self, code, message, actor, attempted_id, **detail):
        self._audit(None, 0, 'rejected', actor, {
            'code': code, 'message': message, 'attempted_schema_id': attempted_id, **detail,
        })
        raise RegistryConflict(code, message, attempted_schema_id=attempted_id, **detail)

    # -- transaction -------------------------------------------------------

    def _transaction(self):
        return _registry_transaction(self.connection)


class _registry_transaction:
    """Commit the audit trail even when registration is rejected.

    A ``RegistryConflict`` is a normal outcome for the registry, and the
    matching audit row is written inside the transaction on purpose.  Any
    other exception aborts the whole registration attempt.
    """

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.execute('BEGIN IMMEDIATE')
        return self

    def __exit__(self, error_type, error, traceback):
        if error_type is None or (error_type is not None and issubclass(error_type, RegistryConflict)):
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


def _definition_id(definition):
    from .schema import schema_id

    return schema_id(definition)


def _safe_definition_id(definition):
    try:
        return _definition_id(definition)
    except Exception:  # noqa: BLE001 - malformed definitions are audited as-is
        return 'unhashable-definition'


def _version_record(row):
    return {
        'schema_id': row['schema_id'],
        'version': row['version'],
        'definition': json.loads(row['definition']),
        'owner': row['owner'],
        'note': row['note'],
        'created_at': row['created_at'],
    }


def field_id_set(definition):
    """All field ids in a definition, including retired descendant paths."""
    result = set()
    for table_definition in definition.values():
        for ids, _, segment in walk_table(table_definition):
            result.add(segment['field_id'])
    return result


def writer_table_definition(record, table):
    definition = record['definition']
    if table not in definition:
        raise RegistryConflict('unknown_table', '写入者版本中没有该数据集合', table=table)
    return definition[table]


def missing_ids(writer_record, current_record):
    """Field ids present in the writer version but removed by the current one."""
    return sorted(field_id_set(writer_record['definition']) - field_id_set(current_record['definition']))
