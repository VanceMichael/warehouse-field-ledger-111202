import argparse
import json
from pathlib import Path

from .catalog import connect, load_file, register
from .domain import overview, rows_at, version_view
from .registry import Registry, RegistryConflict
from .store import ObjectStore


def _parser():
    parser = argparse.ArgumentParser(description='本地数据集合装载、结构版本与查询')
    parser.add_argument('command', choices=[
        'import', 'inspect', 'export', 'register', 'versions', 'view', 'identities', 'audit',
    ])
    parser.add_argument('--db', required=True, help='SQLite 数据库路径')
    parser.add_argument('--input')
    parser.add_argument('--batch')
    parser.add_argument('--objects')
    parser.add_argument('--actor')
    parser.add_argument('--note', default='')
    parser.add_argument('--base-version', type=int)
    parser.add_argument('--base-schema-id')
    parser.add_argument('--writer-version', type=int)
    parser.add_argument('--writer-schema-id')
    parser.add_argument('--version', type=int)
    parser.add_argument('--schema-id')
    parser.add_argument('--table', default='measurements')
    return parser


def main():
    parser = _parser()
    args = parser.parse_args()
    if args.command == 'import' and (not args.input or not args.batch):
        parser.error('导入需要 --input 和 --batch')
    if args.writer_version is not None and args.writer_schema_id is not None:
        parser.error('写入者版本只能指定一项（--writer-version 或 --writer-schema-id）')
    if args.command == 'export' and not args.objects:
        parser.error('导出需要 --objects')
    if args.command == 'register' and not args.input:
        parser.error('注册需要 --input（结构定义 JSON）')
    if args.command == 'register' and not args.actor:
        parser.error('注册需要 --actor')
    if args.command == 'register' and args.base_version is not None and args.base_schema_id is not None:
        if args.base_schema_id != _schema_id_for_version(args.db, args.base_version):
            parser.error('--base-version 与 --base-schema-id 指向不同版本')
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(args.db)
    try:
        result = _run(connection, args, parser)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    finally:
        connection.close()


def _run(connection, args, parser):
    if args.command == 'import':
        try:
            changed = load_file(
                connection, args.input, args.batch,
                writer_version=args.writer_version,
                writer_schema_id=args.writer_schema_id,
                actor=args.actor or 'writer',
            )
        except RegistryConflict as error:
            _dump_conflict(error)
            raise SystemExit(2)
        return {'changed': changed, 'batch': args.batch}
    if args.command == 'inspect':
        return overview(connection, table=args.table)
    if args.command == 'versions':
        return {'versions': Registry(connection)._version_summaries()}
    if args.command == 'identities':
        return {'identities': Registry(connection).identities()}
    if args.command == 'audit':
        return {'audit': Registry(connection).audit(version=args.version)}
    if args.command == 'view':
        return version_view(connection, version=args.version, schema_id=args.schema_id)
    if args.command == 'register':
        definition = json.loads(Path(args.input).read_text(encoding='utf-8'))
        try:
            record = register(
                connection,
                definition,
                actor=args.actor,
                note=args.note,
                base_version=args.base_version,
                base_schema_id=args.base_schema_id,
            )
        except RegistryConflict as error:
            _dump_conflict(error)
            raise SystemExit(2)
        return {
            'version': record['version'],
            'schema_id': record['schema_id'],
            'owner': record['owner'],
            'created_at': record['created_at'],
        }
    return {'object': _export(connection, args)}


def _export(connection, args):
    if args.version is not None or args.schema_id is not None:
        registry = Registry(connection)
        target = registry.get_version(version=args.version, schema_id=args.schema_id)
        records = rows_at(connection, target['version'], table=args.table)
    else:
        records = overview(connection, table=args.table)
    # The original export hashed the flat list of rows; keep that shape.
    return ObjectStore(args.objects).put(records)


def _schema_id_for_version(database, version):
    try:
        connection = connect(database)
    except Exception:  # noqa: BLE001 - the real register call will report it
        return None
    try:
        return Registry(connection).get_version(version=version)['schema_id']
    except RegistryConflict:
        return None
    finally:
        connection.close()


def _dump_conflict(error):
    print(json.dumps({'error': error.as_dict()}, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
