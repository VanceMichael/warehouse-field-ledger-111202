"""Schema definitions, stable field identities and compatibility rules.

A schema is a mapping of table name -> table definition::

    {
      "measurements": {
        "key": ["fld_..."],
        "fields": [
          {
            "name": "sample",
            "field_id": "fld_...",
            "type": "string",          # string | integer | decimal | object
            "nullable": false,
            "nested": null,             # object fields carry {"fields": [...]}
            "default": "..."            # optional, frozen when the version is published
          }
        ]
      }
    }

Field identity is the ``field_id``.  Names are presentation only: a rename
keeps the identity, a dropped name can be reused but the replacement always
gets a fresh identity.  Every schema version is immutable and registered in
SQLite, so projection never has to guess what an old column meant.
"""

import copy
import hashlib
import json
import secrets
from decimal import Decimal, InvalidOperation

TYPES = ('string', 'integer', 'decimal', 'object')
OPTIONAL_SEGMENT_KEYS = {'default'}
UNSET = object()

COMPATIBLE = 'compatible'
INCOMPATIBLE = 'incompatible'
NEUTRAL = 'neutral'


class SchemaError(ValueError):
    """A definition, record or projection is malformed."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def encode_json(value):
    """Canonical JSON bytes; Decimal is written as exact decimal text."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False,
        default=lambda item: format(item, 'f') if isinstance(item, Decimal) else (_raise_type(item)),
    ).encode('utf-8')


def _raise_type(item):
    raise TypeError(f'无法编码的类型: {type(item).__name__}')


def new_field_id():
    return 'fld_' + secrets.token_hex(8)


def derive_field_id(table, path):
    """Deterministic identity for the version-1 baseline (upgrade stability)."""
    material = canonical([table, list(path)]).encode('utf-8')
    return 'fld_' + hashlib.sha256(material).hexdigest()[:16]


def schema_id(definition):
    return 'schema-' + hashlib.sha256(canonical(definition).encode('utf-8')).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------


def _validate_default(segment, where):
    if 'default' not in segment:
        return
    value = segment['default']
    if segment['type'] == 'object':
        raise SchemaError(f'{where} 对象字段不能声明默认值')
    if value is None:
        raise SchemaError(f'{where} 默认值不能是 null；缺失与显式 null 必须保持可区分')
    if segment['type'] == 'string' and not isinstance(value, str):
        raise SchemaError(f'{where} 字符串默认值必须是字符串')
    if segment['type'] == 'integer' and (isinstance(value, bool) or not isinstance(value, int)):
        raise SchemaError(f'{where} 整数默认值必须是整数')
    if segment['type'] == 'decimal':
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise SchemaError(f'{where} 十进制默认值必须是十进制文本或数值')
        try:
            parsed = decimal(value)
        except SchemaError as error:
            raise SchemaError(f'{where} 默认值无效: {error}') from error
        if not parsed.is_finite():
            raise SchemaError(f'{where} 默认值必须是有限值')


def _validate_node(node, where, seen_ids):
    if not isinstance(node, dict) or set(node) != {'fields'} or not isinstance(node['fields'], list):
        raise SchemaError(f'{where} 节点需要 fields 数组')
    names = set()
    for index, segment in enumerate(node['fields']):
        _validate_segment(segment, f'{where}.{index}', seen_ids)
        if segment['name'] in names:
            raise SchemaError(f'{where} 出现重名字段 {segment["name"]}')
        names.add(segment['name'])


def _validate_segment(segment, where, seen_ids):
    if not isinstance(segment, dict) or not set(segment) - OPTIONAL_SEGMENT_KEYS or \
            not {'name', 'field_id', 'type', 'nullable', 'nested'} <= set(segment):
        raise SchemaError(f'{where} 字段需要 name/field_id/type/nullable/nested，可选 default')
    if not isinstance(segment['name'], str) or not segment['name']:
        raise SchemaError(f'{where} 字段名必须是非空字符串')
    if not isinstance(segment['field_id'], str) or not segment['field_id']:
        raise SchemaError(f'{where} 缺少稳定 field_id')
    if segment['field_id'] in seen_ids:
        raise SchemaError(f'{where} field_id 重复: {segment["field_id"]}')
    seen_ids.add(segment['field_id'])
    if segment['type'] not in TYPES:
        raise SchemaError(f'{where} 类型必须是 {TYPES}')
    if not isinstance(segment['nullable'], bool):
        raise SchemaError(f'{where} nullable 必须是布尔值')
    nested = segment['nested']
    if segment['type'] == 'object':
        if nested is None:
            raise SchemaError(f'{where} 对象字段需要 nested 定义')
    elif nested is not None:
        raise SchemaError(f'{where} 只有对象字段可以携带 nested 定义')
    if nested is not None:
        _validate_node(nested, where, seen_ids)
    _validate_default(segment, where)


def validate_definition(definition):
    if not isinstance(definition, dict) or not definition:
        raise SchemaError('结构必须是非空的数据集合映射')
    ids = set()
    for table, table_definition in definition.items():
        if not isinstance(table, str) or not table:
            raise SchemaError('数据集合名必须是非空字符串')
        if not isinstance(table_definition, dict) or set(table_definition) != {'key', 'fields'}:
            raise SchemaError(f'{table} 需要 key 与 fields')
        key = table_definition['key']
        if not isinstance(key, list) or not key or len(set(key)) != len(key):
            raise SchemaError(f'{table} 需要非空且不重复的主键字段')
        _validate_node({'fields': table_definition['fields']}, table, ids)
        leaves = {segment['field_id']: segment for _, _, segment in walk_table(table_definition)
                  if segment['type'] != 'object'}
        for field_id in key:
            if field_id not in leaves:
                raise SchemaError(f'{table} 主键字段 {field_id} 不存在或不是标量')
            if leaves[field_id]['nullable']:
                raise SchemaError(f'{table} 主键字段不能可空')
    return definition


def walk_table(table_definition):
    """Yield ``(id_path, name_path, segment)`` for every field."""
    return _walk_node(table_definition, (), ())


def _walk_node(node, id_path, name_path):
    for segment in node['fields']:
        current_ids = id_path + (segment['field_id'],)
        current_names = name_path + (segment['name'],)
        yield current_ids, current_names, segment
        if segment['nested'] is not None:
            yield from _walk_node(segment['nested'], current_ids, current_names)


def table_map(table_definition):
    """Map ``id_path tuple -> segment``."""
    return {ids: segment for ids, _, segment in walk_table(table_definition)}


def find_field(table_definition, name_path):
    node = table_definition
    segment = None
    for name in name_path:
        matches = [candidate for candidate in node['fields'] if candidate['name'] == name]
        if not matches:
            raise SchemaError('字段路径不存在: ' + '.'.join(name_path))
        segment = matches[0]
        node = segment['nested'] or {}
    if segment is None:
        raise SchemaError('字段路径为空')
    return segment


def copy_definition(definition):
    return copy.deepcopy(definition)


# ---------------------------------------------------------------------------
# Editing helpers (all return new definitions, never mutate in place).
# ---------------------------------------------------------------------------


def rename_field(definition, table, name_path, new_name):
    result = copy.deepcopy(definition)
    segment = find_field(result[table], name_path)
    segment['name'] = new_name
    return result


def add_field(definition, table, parent_names, name, field_type, nullable, default=UNSET,
              field_id=None):
    result = copy.deepcopy(definition)
    node = result[table]
    for parent in parent_names:
        matches = [candidate for candidate in node['fields'] if candidate['name'] == parent]
        if not matches or matches[0]['nested'] is None:
            raise SchemaError('父路径不存在或不是对象: ' + '.'.join(parent_names))
        node = matches[0]['nested']
    if any(candidate['name'] == name for candidate in node['fields']):
        raise SchemaError(f'字段名已存在: {name}')
    segment = {
        'name': name,
        'field_id': field_id or new_field_id(),
        'type': field_type,
        'nullable': bool(nullable),
        'nested': {'fields': []} if field_type == 'object' else None,
    }
    if default is not UNSET:
        segment['default'] = default
    node['fields'].append(segment)
    return result


def drop_field(definition, table, name_path):
    result = copy.deepcopy(definition)
    node = result[table]
    parents = name_path[:-1]
    for parent in parents:
        matches = [candidate for candidate in node['fields'] if candidate['name'] == parent]
        if not matches or matches[0]['nested'] is None:
            raise SchemaError('父路径不存在或不是对象: ' + '.'.join(parents))
        node = matches[0]['nested']
    matches = [candidate for candidate in node['fields'] if candidate['name'] == name_path[-1]]
    if not matches:
        raise SchemaError('字段不存在: ' + '.'.join(name_path))
    node['fields'].remove(matches[0])
    return result


def set_field_type(definition, table, name_path, new_type):
    result = copy.deepcopy(definition)
    segment = find_field(result[table], name_path)
    if new_type == 'object' or segment['type'] == 'object':
        raise SchemaError('对象字段的结构变化需要显式 nested 定义')
    segment['type'] = new_type
    if 'default' in segment and new_type == 'string' and not isinstance(segment['default'], str):
        segment['default'] = str(segment['default'])
    return result


# ---------------------------------------------------------------------------
# Compatibility comparison.
# ---------------------------------------------------------------------------


def compare(previous, current):
    """Describe the change and list blockers.

    Returns ``(operations, blockers)`` where each operation is
    ``(kind, category, detail)``; blockers are the details of incompatible
    operations.  Blockers prevent a commit written against ``previous`` from
    being accepted against ``current``.
    """
    validate_definition(previous)
    validate_definition(current)
    operations = []
    blockers = []
    for table in sorted(set(previous) | set(current)):
        if table not in previous:
            operations.append(('table_added', COMPATIBLE, {'table': table}))
            continue
        if table not in current:
            detail = {'table': table, 'kind': 'table_removed', 'reason': '数据集合被删除'}
            operations.append(('table_removed', INCOMPATIBLE, detail))
            blockers.append(detail)
            continue
        old_table, new_table = previous[table], current[table]
        if sorted(old_table['key']) != sorted(new_table['key']):
            detail = {'table': table, 'kind': 'key_changed', 'reason': '主键身份发生变化'}
            operations.append(('key_changed', INCOMPATIBLE, detail))
            blockers.append(detail)
        _compare_table(table, old_table, new_table, operations, blockers)
    operations.sort(key=lambda item: (item[1], item[0], canonical(item[2])))
    blockers.sort(key=canonical)
    return operations, blockers


def _compare_table(table, old_table, new_table, operations, blockers):
    old_map = table_map(old_table)
    new_map = table_map(new_table)
    old_ids = set(old_map)
    new_ids = set(new_map)

    old_identities = {segment['field_id'] for segment in old_map.values()}
    new_identities = {segment['field_id'] for segment in new_map.values()}
    relocated = old_identities & new_identities
    removed = _top_most(old_ids - new_ids)
    added = _top_most(
        {ids for ids in new_ids - old_ids if new_map[ids]['field_id'] not in relocated}
    )

    for ids in sorted(removed):
        segment = old_map[ids]
        operations.append(('drop', NEUTRAL, {
            'table': table, 'path': '.'.join(_names_for(old_map, ids)),
            'field_id': segment['field_id'], 'name': segment['name'],
        }))

    for ids in sorted(added):
        segment = new_map[ids]
        detail = {
            'table': table, 'path': '.'.join(_names_for(new_map, ids)),
            'field_id': segment['field_id'], 'name': segment['name'],
            'type': segment['type'], 'nullable': segment['nullable'],
        }
        if segment['nullable'] or 'default' in segment \
                or _inside_optional_added_parent(ids, old_ids, new_map):
            # A field is safe to add when a historical file without it still
            # projects: it is nullable, has a frozen default, or sits inside a
            # newly added nullable object whose absence covers it.
            operations.append(('add_nullable', COMPATIBLE, detail))
        else:
            detail = dict(detail, kind='add_required',
                          reason='新增必填字段无法为已写入的历史文件提供值')
            operations.append(('add_required', INCOMPATIBLE, detail))
            blockers.append(detail)

    # An identity that changed parent path was relocated.  Rows are stored by
    # identity path, so the old location's values would be orphaned; moving a
    # field to a different parent is therefore incompatible.
    for ids in removed:
        segment = old_map[ids]
        if segment['field_id'] in relocated:
            moved_to = next(candidate for candidate in new_ids
                            if new_map[candidate]['field_id'] == segment['field_id'])
            detail = {
                'table': table, 'field_id': segment['field_id'],
                'from': '.'.join(_names_for(old_map, ids)),
                'to': '.'.join(_names_for(new_map, moved_to)),
                'kind': 'relocated',
                'reason': '字段身份不能移动到新的父路径，历史值会失去归属',
            }
            operations.append(('relocated', INCOMPATIBLE, detail))
            blockers.append(detail)

    reused_names = {}
    for ids in removed:
        segment = old_map[ids]
        if segment['field_id'] in relocated:
            continue
        reused_names.setdefault(segment['name'], []).append(segment['field_id'])
    for ids in added:
        segment = new_map[ids]
        if segment['field_id'] in relocated:
            continue
        if segment['name'] in reused_names:
            operations.append(('reuse_name', COMPATIBLE, {
                'table': table,
                'name': segment['name'],
                'retired_field_id': reused_names[segment['name']][0],
                'field_id': segment['field_id'],
                'path': '.'.join(_names_for(new_map, ids)),
            }))

    for ids in sorted(old_ids & new_ids):
        old, new = old_map[ids], new_map[ids]
        if old['name'] != new['name']:
            operations.append(('rename', COMPATIBLE, {
                'table': table,
                'field_id': old['field_id'],
                'from': '.'.join(_names_for(old_map, ids)),
                'to': '.'.join(_names_for(new_map, ids)),
            }))
        if old['type'] != new['type']:
            detail = {
                'table': table, 'path': '.'.join(_names_for(new_map, ids)),
                'field_id': old['field_id'], 'from': old['type'], 'to': new['type'],
            }
            if _widens(old['type'], new['type']):
                operations.append(('widen', COMPATIBLE, detail))
            else:
                detail = dict(detail, kind='narrow',
                              reason='类型收窄或不是整数到精确十进制的扩大')
                operations.append(('narrow', INCOMPATIBLE, detail))
                blockers.append(detail)
        if old['nullable'] and not new['nullable']:
            detail = {
                'table': table, 'path': '.'.join(_names_for(new_map, ids)),
                'field_id': old['field_id'], 'kind': 'nullable_to_required',
                'reason': '历史文件中的显式 null 无法投影为必填值',
            }
            operations.append(('nullable_to_required', INCOMPATIBLE, detail))
            blockers.append(detail)
        elif new['nullable'] and not old['nullable']:
            operations.append(('nullable_relaxed', COMPATIBLE, {
                'table': table,
                'path': '.'.join(_names_for(new_map, ids)),
                'field_id': old['field_id'],
            }))
        if canonical(old.get('default', None)) != canonical(new.get('default', None)):
            operations.append(('default_changed', COMPATIBLE, {
                'table': table,
                'path': '.'.join(_names_for(new_map, ids)),
                'field_id': old['field_id'],
                'from': old.get('default'),
                'to': new.get('default'),
            }))


def _inside_optional_added_parent(ids, old_ids, new_map):
    """True when the whole parent branch was added and is nullable."""
    if len(ids) < 2:
        return False
    for length in range(1, len(ids)):
        prefix = ids[:length]
        if prefix in old_ids:
            return False
        if not new_map[prefix]['nullable']:
            return False
    return True


def _top_most(ids):
    """Keep only ids whose ancestors are not also in the set."""
    result = []
    for ids in ids:
        if not any(ids[:length] in ids for length in range(1, len(ids))):
            result.append(ids)
    return result


def _names_for(mapping, ids):
    """Name path for ``ids``; every prefix of a valid id path is in the map."""
    result = []
    for length in range(1, len(ids) + 1):
        prefix = ids[:length]
        if prefix not in mapping:
            raise SchemaError('内部错误: 字段路径不存在')
        result.append(mapping[prefix]['name'])
    return tuple(result)


def _widens(old_type, new_type):
    return old_type == 'integer' and new_type == 'decimal'


def summarize(operations):
    lines = []
    for kind, category, detail in operations:
        table = detail.get('table', '')
        if kind == 'rename':
            lines.append(f'{table}:{detail["from"]} -> {detail["to"]}（字段 {detail["field_id"]}）')
        elif kind == 'drop':
            lines.append(f'{table}:删除 {detail["path"]}（字段 {detail["field_id"]}）')
        elif kind == 'reuse_name':
            lines.append(f'{table}:名字 {detail["name"]} 重新使用，新字段 {detail["field_id"]}（旧 {detail["retired_field_id"]}）')
        elif kind == 'add_required':
            lines.append(f'{table}:新增必填 {detail["path"]}')
        elif kind == 'add_nullable':
            lines.append(f'{table}:新增可空 {detail["path"]}')
        elif kind == 'narrow':
            lines.append(f'{table}:{detail["path"]} 类型收窄 {detail["from"]} -> {detail["to"]}')
        elif kind == 'nullable_to_required':
            lines.append(f'{table}:{detail["path"]} 可空改为必填')
        elif kind == 'widen':
            lines.append(f'{table}:{detail["path"]} 扩大 {detail["from"]} -> {detail["to"]}')
        elif kind == 'default_changed':
            lines.append(f'{table}:{detail["path"]} 默认值 {detail["from"]!r} -> {detail["to"]!r}')
        else:
            lines.append(f'{table}:{kind} {detail.get("path", detail.get("name", ""))}'.strip())
    return '; '.join(sorted(lines))


def change_signature(operations):
    kinds = [[kind, category, detail] for kind, category, detail in operations]
    return hashlib.sha256(canonical(kinds).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Typed values: encoding at write time, decoding at projection time.
# ---------------------------------------------------------------------------


def decimal(value):
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise SchemaError('布尔值不是十进制值')
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise SchemaError('十进制值必须是有限值')
        return Decimal(repr(value))
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise SchemaError(f'无效的十进制值: {value!r}') from error
        if not parsed.is_finite():
            raise SchemaError('十进制值必须是有限值')
        return parsed
    raise SchemaError(f'十进制值类型无效: {type(value).__name__}')


def decimal_text(value):
    """Exact decimal text preserving the writer's declared scale."""
    return format(decimal(value), 'f')


def encode_value(segment, value, where):
    """Encode one JSON value as ``{"t": type, "v": exact value}``."""
    if value is None:
        if not segment['nullable']:
            raise SchemaError(f'{where} 必填字段不能是 null')
        return {'t': 'null'}
    if segment['type'] == 'object':
        if not isinstance(value, dict):
            raise SchemaError(f'{where} 对象字段需要映射')
        return {'t': 'object'}
    if segment['type'] == 'string':
        if not isinstance(value, str):
            raise SchemaError(f'{where} 字符串字段需要字符串')
        return {'t': 'string', 'v': value}
    if segment['type'] == 'integer':
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f'{where} 整数字段需要整数')
        return {'t': 'integer', 'v': value}
    if segment['type'] == 'decimal':
        return {'t': 'decimal', 'v': decimal_text(value)}
    raise SchemaError(f'{where} 未知类型 {segment["type"]}')


def decode_value(segment, entry, where):
    """Decode a stored typed value for the target segment."""
    kind = entry.get('t')
    if kind == 'null':
        if not segment['nullable']:
            raise SchemaError(f'{where} 必填字段收到显式 null')
        return None
    if segment['type'] == 'decimal':
        if kind == 'decimal':
            return entry['v']
        if kind == 'integer':
            return format(Decimal(entry['v']), 'f')
        raise SchemaError(f'{where} 无法把 {kind} 投影为十进制')
    if segment['type'] == 'integer':
        if kind != 'integer':
            raise SchemaError(f'{where} 无法把 {kind} 投影为整数')
        return entry['v']
    if segment['type'] == 'string':
        if kind != 'string':
            raise SchemaError(f'{where} 无法把 {kind} 投影为字符串')
        return entry['v']
    raise SchemaError(f'{where} 未知类型 {segment["type"]}')


def default_value(segment):
    """Render the frozen default of a target segment."""
    value = segment['default']
    if segment['type'] == 'decimal':
        return decimal_text(value)
    return value
