"""Transactional storage, ingest and projection.

Rows are stored as version-stamped typed records keyed by field identity.
Nothing is projected at write time: reads resolve a writer version to a target
version by identity, so a file written before a rename stays readable, and a
writer's ``temperature`` can never collide with a string note that later reused
the name.  Legacy flat rows are adopted into the baseline once at startup;
that adoption is the only place a column name is trusted.
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .definition import initial_definition
from .registry import Registry, RegistryConflict, now_text
from .schema import SchemaError, canonical, encode_json, encode_value, walk_table


SCHEMA_STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS schema_versions (
        schema_id TEXT NOT NULL,
        version INTEGER PRIMARY KEY,
        definition TEXT NOT NULL,
        owner TEXT NOT NULL,
        note TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS schema_field_identities (
        field_id TEXT PRIMARY KEY,
        table_name TEXT NOT NULL,
        name_path TEXT NOT NULL,
        first_version INTEGER NOT NULL,
        retired_version INTEGER
    )''',
    '''CREATE TABLE IF NOT EXISTS schema_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        schema_id TEXT,
        version INTEGER NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS ingest_batches (
        batch TEXT PRIMARY KEY,
        digest TEXT NOT NULL,
        rows INTEGER NOT NULL,
        schema_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        written_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS ingest_batches_audit (
        attempt INTEGER PRIMARY KEY AUTOINCREMENT,
        batch TEXT NOT NULL,
        digest TEXT NOT NULL,
        schema_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        detail TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS stored_rows (
        table_name TEXT NOT NULL,
        schema_id TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        record TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        PRIMARY KEY (table_name, key_hash)
    )''',
    '''CREATE TABLE IF NOT EXISTS stored_row_batches (
        table_name TEXT NOT NULL,
        batch TEXT NOT NULL,
        schema_id TEXT NOT NULL,
        key_hash TEXT NOT NULL,
        PRIMARY KEY (table_name, batch, key_hash)
    )''',
    '''CREATE TABLE IF NOT EXISTS registry_markers (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''',
)


LEGACY_TABLE = 'measurements'


def encode(value):
    return canonical(value).encode('utf-8')


def connect(path):
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=5000')
    for statement in SCHEMA_STATEMENTS:
        connection.execute(statement)
    _initialise(connection)
    return connection


def _initialise(connection):
    with transaction(connection):
        if connection.execute('SELECT COUNT(*) FROM schema_versions').fetchone()[0] == 0:
            registry = Registry(connection)
            registry.bootstrap_in_transaction(initial_definition())
        _adopt_legacy(connection)
        _refresh_flat_view(connection)
        connection.execute(
            'INSERT OR REPLACE INTO registry_markers VALUES (?, ?)', ('flat_view_owned', '1')
        )


def _flat_columns(table_definition):
    return [segment for segment in table_definition['fields'] if segment['type'] != 'object']


def _refresh_flat_view(connection):
    """Rebuild the name-keyed flat table from stored rows.

    Called at startup and after a registration changes names or shapes.  It
    is derived data: every value comes from a stored record projected into
    the current version, so no second source of truth exists.
    """
    from .projection import project_node, stored_values

    current = Registry(connection).current()
    table_definition = current['definition'][LEGACY_TABLE]
    scalars = _flat_columns(table_definition)
    columns = [segment['name'] for segment in scalars]
    connection.execute(f'DROP TABLE IF EXISTS "{LEGACY_TABLE}"')
    declarations = ', '.join(_sql_declaration(segment, table_definition['key']) for segment in scalars)
    keys = [segment['name'] for segment in scalars if segment['field_id'] in table_definition['key']]
    key_clause = ''
    if keys:
        quoted_keys = ', '.join(f'"{name}"' for name in keys)
        key_clause = f', PRIMARY KEY ({quoted_keys})'
    connection.execute(f'CREATE TABLE "{LEGACY_TABLE}" ({declarations}{key_clause})')
    for row in connection.execute(
        'SELECT record FROM stored_rows WHERE table_name=?', (LEGACY_TABLE,)
    ).fetchall():
        projected = project_node(
            {'fields': table_definition['fields']}, stored_values(row['record']), (), current['version']
        )
        _insert_flat_row(connection, columns, projected)


def _insert_flat_row(connection, columns, projected):
    values = [projected.get(name) for name in columns]
    names = ', '.join(f'"{name}"' for name in columns)
    placeholders = ', '.join('?' for _ in columns)
    connection.execute(f'INSERT OR REPLACE INTO "{LEGACY_TABLE}" ({names}) VALUES ({placeholders})', values)


def _sync_flat_row(connection, table_definition, encoded):
    """Mirror one just-committed record into the flat SQL table."""
    from .projection import project_node

    values = {tuple(value['ids']): value for value in encoded}
    projected = project_node({'fields': table_definition['fields']}, values, ())
    columns = [segment['name'] for segment in _flat_columns(table_definition)]
    if any(name not in projected for name in columns):
        return
    _insert_flat_row(connection, columns, projected)


def _sql_declaration(segment, key):
    field_type = 'INTEGER' if segment['type'] == 'integer' else 'TEXT'
    if segment['field_id'] in key:
        return f'"{segment["name"]}" {field_type} NOT NULL'
    return f'"{segment["name"]}" {field_type}'


def _adopt_legacy(connection):
    """Take pre-registry rows under version 1 exactly once.

    Legacy tables called themselves ``measurements`` with ``sample, device,
    temperature, unit``.  The baseline identity of those columns is derived
    deterministically, so upgrading a live database keeps the old files
    readable without a runtime name guess.
    """
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (LEGACY_TABLE,)
    ).fetchone() is None:
        return
    markers = {
        row['name'] for row in connection.execute('SELECT name FROM registry_markers')
    }
    if 'legacy_adopted' in markers or 'flat_view_owned' in markers:
        return
    current = Registry(connection).current()
    columns = [info['name'] for info in connection.execute(f'PRAGMA table_info("{LEGACY_TABLE}")')]
    known = {segment['name'] for segment in current['definition'][LEGACY_TABLE]['fields']}
    if not set(columns) <= known:
        raise RegistryConflict(
            'legacy_table_mismatch',
            '旧表包含基线无法识别的列；请先登记包含这些字段的结构',
            columns=sorted(set(columns) - known),
        )
    rows = [dict(record) for record in connection.execute(f'SELECT * FROM "{LEGACY_TABLE}"')]
    for row in rows:
        encoded = encode_record(LEGACY_TABLE, current['definition'][LEGACY_TABLE], row, 'legacy')
        key_hash = hash_key(record_key(current['definition'][LEGACY_TABLE], row))
        _insert_record(connection, LEGACY_TABLE, current['schema_id'], key_hash, encoded,
                       current['created_at'])
    if rows:
        connection.execute(
            'INSERT OR REPLACE INTO ingest_batches VALUES (?, ?, ?, ?, ?, ?)',
            ('legacy', hashlib.sha256(encode(rows)).hexdigest(), len(rows),
             current['schema_id'], current['version'], current['created_at']),
        )
    connection.execute('INSERT INTO registry_markers VALUES (?, ?)', ('legacy_adopted', current['schema_id']))
    connection.execute('DROP TABLE "measurements"')


@contextmanager
def transaction(connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


# ---------------------------------------------------------------------------
# Record validation and encoding (against the writer's version).
# ---------------------------------------------------------------------------


def _name_paths(table_definition):
    return {'.'.join(names): (ids, segment) for ids, names, segment in walk_table(table_definition)}


def encode_record(table, table_definition, row, where):
    """Validate ``row`` and encode it by identity path.

    Nested fields are written as dotted names (``instrument.serial``) because
    JSON objects nest naturally; both ``{"instrument": {"serial": 1}}`` and
    a flat dotted key are accepted, and output is keyed by identity only.
    """
    if not isinstance(row, dict):
        raise SchemaError(f'{where} 记录必须是映射')
    flat = flatten_source(row, where)
    paths = _name_paths(table_definition)
    unknown = set(flat) - set(paths)
    if unknown:
        raise SchemaError(f'{where} 记录字段与已登记结构不一致: ' + ', '.join(sorted(unknown)))
    values = []

    def visit(node, ids_prefix, name_prefix, parent_present):
        for segment in node['fields']:
            ids = ids_prefix + (segment['field_id'],)
            names = name_prefix + (segment['name'],)
            dotted = '.'.join(names)
            if not parent_present:
                values.append({'ids': list(ids), 'names': list(names), 't': 'missing'})
                continue
            if segment['type'] == 'object':
                has_value = dotted in flat
                has_children = any(key.startswith(dotted + '.') for key in flat)
                if not has_value and not has_children:
                    if not segment['nullable']:
                        raise SchemaError(f'{where} 必填对象字段 {dotted} 缺失')
                    values.append({'ids': list(ids), 'names': list(names), 't': 'missing'})
                    continue
                if has_value and not isinstance(flat[dotted], dict):
                    raise SchemaError(f'{where} 对象字段 {dotted} 需要映射')
                values.append({'ids': list(ids), 'names': list(names), 't': 'object'})
                visit(segment['nested'], ids, names, True)
                continue
            if dotted not in flat:
                if not segment['nullable'] and 'default' not in segment:
                    raise SchemaError(f'{where} 必填字段 {dotted} 缺失')
                values.append({'ids': list(ids), 'names': list(names), 't': 'missing'})
                continue
            encoded = encode_value(segment, flat[dotted], f'{where}.{dotted}')
            values.append({'ids': list(ids), 'names': list(names), **encoded})

    visit({'fields': table_definition['fields']}, (), (), True)
    return values


def flatten_source(row, where):
    """Accept nested objects and dotted keys, producing one flat mapping."""
    flat = {}

    def visit(mapping, prefix):
        for key, value in mapping.items():
            if not isinstance(key, str) or not key:
                raise SchemaError(f'{where} 字段名必须是非空字符串')
            dotted = f'{prefix}{key}'
            if isinstance(value, dict):
                if not value:
                    flat[dotted] = {}
                else:
                    visit(value, dotted + '.')
            else:
                if dotted in flat:
                    raise SchemaError(f'{where} 字段重复: {dotted}')
                flat[dotted] = value

    visit(row, '')
    return flat


def record_key(table_definition, row):
    """Canonical key text built from identity and exact key values."""
    flat = flatten_source(row, 'record')
    paths = _name_paths(table_definition)
    parts = []
    for field_id in table_definition['key']:
        match = [dotted for dotted, (ids, segment) in paths.items()
                 if segment['field_id'] == field_id and segment['type'] != 'object']
        if not match:
            raise SchemaError(f'主键字段 {field_id} 不存在')
        dotted = match[0]
        if dotted not in flat:
            raise SchemaError(f'主键字段 {dotted} 缺失')
        encoded = encode_value(paths[dotted][1], flat[dotted], f'key.{dotted}')
        parts.append([field_id, encoded])
    return canonical(parts)


def hash_key(key_text):
    return hashlib.sha256(key_text.encode('utf-8')).hexdigest()


def _id_path(ids):
    return '/'.join(ids)


def _insert_record(connection, table, schema_record_id, key_hash, encoded, at):
    payload = encode_json({
        'values': {_id_path(value['ids']): value for value in encoded},
    }).decode('utf-8')
    connection.execute(
        'INSERT INTO stored_rows (table_name, schema_id, key_hash, record, committed_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (table, schema_record_id, key_hash, payload, at),
    )


# ---------------------------------------------------------------------------
# Producer writes.
# ---------------------------------------------------------------------------


def load_bundle(connection, bundle, batch, writer_version=None, writer_schema_id=None, actor='writer'):
    return write_bundle(
        connection, bundle, batch,
        writer_version=writer_version,
        writer_schema_id=writer_schema_id,
        actor=actor,
    )['changed']


def prepare_file(connection, bundle, batch, writer_version=None, writer_schema_id=None,
                 actor='writer'):
    """Producer-side step: fix the authoring version and digest the file.

    A producer calls this before handing the file over, keeps the returned
    token, and passes it to :func:`write_bundle` at commit time.  Reading the
    version here is not binding on its own; the commit re-checks it inside
    the write transaction.
    """
    if not isinstance(bundle, dict) or not isinstance(batch, str) or not batch:
        raise ValueError('输入需要命名数据集合与批次标识')
    registry = Registry(connection)
    writer = _resolve_writer(registry, writer_version, writer_schema_id)
    return {
        'batch': batch,
        'digest': _digest(bundle, batch, writer),
        'writer_version': writer['version'],
        'writer_schema_id': writer['schema_id'],
        'actor': actor,
    }


def register(connection, definition, actor, note='', base_version=None, base_schema_id=None):
    """Register a new version and rebuild derived flat access in one transaction.

    A rejection commits the registry's audit row and nothing else, then
    re-raises the stable conflict.
    """
    registry = Registry(connection)
    with audit_retaining_transaction(connection):
        record = registry.register_in_transaction(
            definition, actor=actor, note=note,
            base_version=base_version, base_schema_id=base_schema_id,
        )
        _refresh_flat_view(connection)
    return record


@contextmanager
def audit_retaining_transaction(connection):
    """Commit audit-only attempts; roll back anything else.

    Called only after all validation, so a ``RegistryConflict`` at this point
    means the transaction contains nothing but the audit row that explains
    the refusal.
    """
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield connection
    except RegistryConflict:
        connection.commit()
        raise
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def write_bundle(connection, bundle, batch, writer_version=None, writer_schema_id=None,
                 actor='writer', writer_token=None):
    """Commit a producer file after validating its declared writer version.

    A ``writer_token`` from :func:`prepare_file` is re-checked against the
    registry inside the write transaction, so a registration that landed in
    between produces a deterministic rejection instead of a partial commit.
    Every rejection is recorded in ``ingest_batches_audit`` under a stable
    code; nothing enters ``stored_rows`` or ``ingest_batches`` beyond the
    audit row.
    """
    if not isinstance(bundle, dict) or not isinstance(batch, str) or not batch:
        raise ValueError('输入需要命名数据集合与批次标识')
    if not isinstance(actor, str) or not actor:
        raise RegistryConflict('invalid_actor', '写入需要非空的执行者标识')
    if writer_token is not None:
        writer_version = writer_token['writer_version']
        writer_schema_id = writer_token['writer_schema_id']
        batch = writer_token['batch']
        actor = writer_token.get('actor', actor)
    registry = Registry(connection)
    with audit_retaining_transaction(connection):
        current = registry.current()
        if current is None:
            raise RegistryConflict('uninitialised', '仓库尚未登记结构基线')
        try:
            writer = _resolve_writer(registry, writer_version, writer_schema_id)
        except RegistryConflict as error:
            _record_attempt(connection, batch, 'unknown', {'schema_id': 'unknown', 'version': 0},
                            'rejected', error.code, {'message': str(error), **error.detail})
            raise
        digest = _digest(bundle, batch, writer)
        if writer_token is not None and writer_token['digest'] != digest:
            raise ValueError('写入令牌与文件内容不一致')

        unknown = set(bundle) - set(current['definition'])
        if unknown:
            raise ValueError('来源包含未登记的数据集合: ' + ', '.join(sorted(unknown)))
        if set(bundle) - set(writer['definition']):
            raise RegistryConflict('unknown_table', '写入者版本中没有该数据集合',
                                   tables=sorted(set(bundle) - set(writer['definition'])))
        prepared = _prepare_rows(writer, bundle)

        previous = connection.execute('SELECT digest FROM ingest_batches WHERE batch=?', (batch,)).fetchone()
        if previous is not None:
            if previous['digest'] != digest:
                raise ValueError('同批次内容不一致')
            return {'changed': False, 'batch': batch, 'digest': digest,
                    'schema_id': writer['schema_id'], 'version': writer['version']}

        refusal = _writer_refusal(writer, current, prepared)
        if refusal is not None:
            code, detail = refusal
            _record_attempt(connection, batch, digest, writer, 'rejected', code, detail)
            raise RegistryConflict(code, detail['message'], **{
                key: value for key, value in detail.items() if key != 'message'
            })

        conflict = _existing_keys(connection, writer, prepared)
        if conflict is not None:
            _record_attempt(connection, batch, digest, writer, 'rejected', 'duplicate_key', conflict)
            raise sqlite3.IntegrityError(conflict['message'])

        at = now_text()
        inserted = 0
        for table, rows in prepared:
            table_definition = writer['definition'][table]
            if table not in current['definition']:
                raise RegistryConflict('unknown_table', '当前版本没有该数据集合', table=table)
            target_definition = current['definition'][table]
            for row in rows:
                encoded = encode_record(table, table_definition, row, 'record')
                key_hash = hash_key(record_key(table_definition, row))
                _insert_record(connection, table, writer['schema_id'], key_hash, encoded, at)
                connection.execute('INSERT INTO stored_row_batches VALUES (?, ?, ?, ?)',
                                   (table, batch, writer['schema_id'], key_hash))
                if table == LEGACY_TABLE:
                    _sync_flat_row(connection, target_definition, encoded)
                inserted += 1
        connection.execute(
            'INSERT INTO ingest_batches VALUES (?, ?, ?, ?, ?, ?)',
            (batch, digest, inserted, writer['schema_id'], writer['version'], at),
        )
        _record_attempt(connection, batch, digest, writer, 'committed', 'ok',
                        {'message': '已提交', 'rows': inserted})
    return {'changed': True, 'batch': batch, 'digest': digest,
            'schema_id': writer['schema_id'], 'version': writer['version'], 'rows': inserted}


def _digest(bundle, batch, writer):
    return hashlib.sha256(canonical({
        'batch': batch,
        'writer_version': writer['version'],
        'writer_schema_id': writer['schema_id'],
        'bundle': bundle,
    }).encode('utf-8')).hexdigest()


def _resolve_writer(registry, writer_version, writer_schema_id):
    if writer_version is None and writer_schema_id is None:
        return registry.current()
    if writer_schema_id is not None:
        return registry.get_version(schema_id=writer_schema_id)
    if isinstance(writer_version, bool) or not isinstance(writer_version, int):
        raise RegistryConflict('invalid_version', '版本号必须是整数', version=writer_version)
    return registry.get_version(version=writer_version)


def _prepare_rows(writer, bundle):
    prepared = []
    for table, rows in bundle.items():
        if not isinstance(rows, list):
            raise ValueError('数据集合必须是记录数组')
        table_definition = writer['definition'][table]
        seen = set()
        for row in rows:
            key = record_key(table_definition, row)
            if key in seen:
                raise sqlite3.IntegrityError(f'批次内主键重复: {table}')
            seen.add(key)
        prepared.append((table, rows))
    return prepared


def _writer_refusal(writer, current, prepared):
    """Decide whether a file written under ``writer`` may enter ``current``.

    An older writer version is fine when every identity it used still exists
    and every required current field it lacks has a frozen default.  A field
    the writer used that was dropped (its identity retired) makes the file
    unsafe, because its column would have to be reinterpreted by name.
    """
    if writer['schema_id'] == current['schema_id']:
        return None

    from .registry import missing_ids

    removed = missing_ids(writer, current)
    if removed:
        return 'incompatible_writer_version', {
            'message': '写者版本中的字段已在当前版本删除，历史文件无法安全投影',
            'writer_version': writer['version'], 'current_version': current['version'],
            'writer_schema_id': writer['schema_id'], 'current_schema_id': current['schema_id'],
            'removed_field_ids': removed,
        }

    target = current['definition']
    for table, rows in prepared:
        if table not in target:
            return 'unknown_table', {
                'message': f'当前版本没有数据集合 {table}', 'table': table,
                'writer_version': writer['version'], 'current_version': current['version'],
            }
        writer_paths = _name_paths(writer['definition'][table])
        target_paths = _name_paths(target[table])
        writer_ids = {segment['field_id'] for _, segment in writer_paths.values()}
        target_ids = {segment['field_id'] for _, segment in target_paths.values()}
        orphan = writer_ids - target_ids - set(removed)
        if orphan:
            return 'incompatible_writer_version', {
                'message': '写者字段在当前版本中没有对应的身份',
                'writer_version': writer['version'], 'current_version': current['version'],
                'field_ids': sorted(orphan),
            }
        for _, target_segment in target_paths.values():
            if target_segment['type'] == 'object' or target_segment['nullable']:
                continue
            if 'default' in target_segment:
                continue
            if target_segment['field_id'] not in writer_ids:
                return 'incompatible_writer_version', {
                    'message': '当前版本新增的必填字段没有默认值，旧写者无法满足',
                    'writer_version': writer['version'], 'current_version': current['version'],
                    'field_id': target_segment['field_id'],
                }
    return None


def _existing_keys(connection, writer, prepared):
    for table, rows in prepared:
        table_definition = writer['definition'][table]
        for row in rows:
            key_hash = hash_key(record_key(table_definition, row))
            if connection.execute(
                'SELECT 1 FROM stored_rows WHERE table_name=? AND key_hash=?', (table, key_hash)
            ).fetchone() is not None:
                return {'message': f'主键冲突: {table}', 'table': table}
    return None


def _record_attempt(connection, batch, digest, writer, status, reason, detail):
    connection.execute(
        'INSERT INTO ingest_batches_audit '
        '(batch, digest, schema_id, version, status, reason, detail, attempted_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (batch, digest, writer['schema_id'], writer['version'], status, reason,
         encode_json(detail).decode('utf-8'), now_text()),
    )


def load_file(connection, path, batch, writer_version=None, writer_schema_id=None, actor='writer'):
    return load_bundle(
        connection,
        json.loads(Path(path).read_text(encoding='utf-8')),
        batch,
        writer_version=writer_version,
        writer_schema_id=writer_schema_id,
        actor=actor,
    )
