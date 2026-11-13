"""Projection of stored files into a target schema version.

A stored row is a flat list of typed values keyed by identity path.  Projection
walks the *target* version and distinguishes three cases for every target
field:

``missing``
    The writer had no such field.  If the target declares a default, that
    frozen version-time default is emitted; if the target is nullable, null is
    emitted; otherwise projection is impossible and fails loudly.
``null``
    The writer explicitly wrote JSON null.  It stays null and is never
    replaced by a default.
``value``
    A typed value from the writer, decoded into the target representation.

Defaults are read from the registered target definition only, so a default
frozen when the version was published does not drift at read time.
"""

import json

from .schema import SchemaError, decode_value, default_value, walk_table


MISSING = object()



def stored_values(record):
    """Identity-path tuple -> typed entry for one stored row."""
    entries = json.loads(record)['values']
    return {tuple(key.split('/')): value for key, value in entries.items()}


def project_node(node, values, ids_prefix=(), target_version=None, trace=None, name_prefix=()):
    """Build the target JSON node, following target identities."""
    result = {}
    for segment in node['fields']:
        ids = ids_prefix + (segment['field_id'],)
        names = name_prefix + (segment['name'],)
        entry = values.get(ids, MISSING)
        if entry is MISSING or entry['t'] == 'missing':
            if 'default' in segment:
                result[segment['name']] = default_value(segment)
                status = 'default'
            elif segment['nullable']:
                result[segment['name']] = None
                status = 'missing'
            else:
                raise SchemaError(
                    '字段 {path} 在写者版本中缺失且没有默认值（版本 {version}）'.format(
                        path='.'.join(names), version=target_version
                    )
                )
            if trace is not None:
                trace.append({'field_id': ids[-1], 'path': '.'.join(names), 'source': status})
            continue
        if entry['t'] == 'null':
            if not segment['nullable']:
                raise SchemaError(f'字段 {".".join(names)} 收到显式 null 但目标版本不允许')
            result[segment['name']] = None
            if trace is not None:
                trace.append({'field_id': ids[-1], 'path': '.'.join(names), 'source': 'null'})
            continue
        if segment['type'] == 'object':
            if entry['t'] != 'object':
                raise SchemaError(f'对象字段 {".".join(names)} 的值类型不符')
            child_trace = [] if trace is not None else None
            result[segment['name']] = project_node(
                segment['nested'], values, ids, target_version, child_trace, names,
            )
            if trace is not None:
                trace.append({'field_id': ids[-1], 'path': '.'.join(names), 'source': 'object'})
                trace.extend(child_trace)
            continue
        result[segment['name']] = decode_value(segment, entry, '.'.join(names))
        if trace is not None:
            trace.append({'field_id': ids[-1], 'path': '.'.join(names), 'source': 'value'})
    return result


def projection_report(writer_definition, target_definition, table='measurements'):
    """Per target field, where a value written under ``writer_definition`` comes from."""
    if table not in writer_definition or table not in target_definition:
        return []
    writer_ids = {ids for ids, _, _ in walk_table(writer_definition[table])}
    report = []
    for ids, names, segment in walk_table(target_definition[table]):
        if ids in writer_ids:
            source = 'carried'
        elif 'default' in segment:
            source = 'default'
        elif segment['nullable']:
            source = 'missing_as_null'
        else:
            source = 'unavailable'
        item = {
            'field_id': ids[-1],
            'path': '.'.join(names),
            'target_type': segment['type'],
            'source': source,
        }
        if source == 'default':
            item['default'] = default_value(segment)
        report.append(item)
    return report
