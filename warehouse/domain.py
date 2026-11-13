"""Read paths over the warehouse.

``overview`` keeps the original flat contract exactly: current names, current
values, ordered by key.  ``rows_at`` projects stored files into a requested
version.  ``version_view`` reports every stored batch with the version that
wrote it, how it resolves under the requested version, and the identity-level
operations between versions.
"""

from .projection import project_node, projection_report, stored_values
from .registry import Registry
from .schema import SchemaError, table_map


def _table(record, table):
    if table not in record['definition']:
        raise SchemaError(f'结构版本 {record["version"]} 没有数据集合 {table}')
    return record['definition'][table]


def _stored(connection, table, batch=None):
    if batch is None:
        rows = connection.execute(
            'SELECT schema_id, record FROM stored_rows WHERE table_name=?', (table,)
        ).fetchall()
    else:
        rows = connection.execute(
            'SELECT r.schema_id, r.record FROM stored_rows AS r '
            'JOIN stored_row_batches AS b ON b.table_name=r.table_name '
            'AND b.key_hash=r.key_hash WHERE b.batch=? AND r.table_name=?',
            (batch, table),
        ).fetchall()
    return [(row['schema_id'], row['record']) for row in rows]


def _project_rows(rows, target_table, target_version):
    projected = []
    for schema_id, payload in rows:
        values = stored_values(payload)
        try:
            node = project_node({'fields': target_table['fields']}, values, (), target_version)
        except SchemaError as error:
            raise SchemaError(f'写入版本 {schema_id} 无法投影到版本 {target_version}: {error}') from error
        projected.append(node)
    return projected


def project(connection, version=None, schema_id=None, table='measurements', batch=None):
    """Project stored rows into ``version`` (default: current)."""
    registry = Registry(connection)
    target = registry.current() if version is None and schema_id is None else \
        registry.get_version(version=version, schema_id=schema_id)
    target_table = _table(target, table)
    return _project_rows(_stored(connection, table, batch=batch), target_table, target['version'])


def overview(connection, table='measurements'):
    """The unchanged flat read: current names, current values, key order."""
    registry = Registry(connection)
    target = registry.current()
    table_definition = _table(target, table)
    rows = project(connection, table=table)
    segments = table_map(table_definition)
    key_names = [segments[(field_id,)]['name'] for field_id in table_definition['key']]
    return sorted(rows, key=lambda row: tuple(
        (row.get(name) is None, str(row.get(name))) for name in key_names
    ))


def rows_at(connection, version, table='measurements'):
    """All stored files, projected into a specific registered version."""
    return project(connection, version=version, table=table)


def version_view(connection, version=None, schema_id=None):
    """Structured view of file identity across registered versions.

    For each stored batch it reports the writing version, the projected rows
    under the requested version, and per-target-field sourcing.  It also
    reports identity operations (renames, nested renames, drops, name reuse)
    and the identity ledger, so a reused name is visibly a different field.
    """
    registry = Registry(connection)
    if version is None and schema_id is None:
        target = registry.current()
    else:
        target = registry.get_version(version=version, schema_id=schema_id)
    versions = registry.versions()

    operations = []
    for entry in registry.audit():
        if entry['kind'] != 'registered':
            continue
        for item in entry['detail'].get('operations', []):
            operations.append({
                'version': entry['version'],
                'kind': item['kind'],
                'category': item['category'],
                'detail': item['detail'],
            })

    batch_detail = []
    for row in connection.execute(
        'SELECT batch, digest, rows, schema_id, version, written_at FROM ingest_batches '
        'ORDER BY written_at, batch'
    ):
        writer = registry.get_version(version=row['version'])
        stored = _stored(connection, 'measurements', batch=row['batch'])
        projected = _project_rows(stored, target['definition']['measurements'], target['version'])
        batch_detail.append({
            'batch': row['batch'],
            'digest': row['digest'],
            'rows': row['rows'],
            'written_version': row['version'],
            'written_schema_id': row['schema_id'],
            'written_at': row['written_at'],
            'projects_at': target['version'],
            'sources': projection_report(writer['definition'], target['definition']),
            'projected': projected,
        })

    return {
        'version': target['version'],
        'schema_id': target['schema_id'],
        'definition': target['definition'],
        'versions': [
            {'version': item['version'], 'schema_id': item['schema_id'],
             'owner': item['owner'], 'note': item['note'], 'created_at': item['created_at']}
            for item in versions
        ],
        'operations': operations,
        'identities': registry.identities(),
        'audit': registry.audit(),
        'batches': batch_detail,
    }
