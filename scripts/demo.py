"""End-to-end demonstration on a fresh temporary directory.

Runs the real CLI: import, replay, inspect, export, then a schema evolution
(rename, a producer still on the old version, a rejected incompatible
registration) and the version view.  Nothing is pre-recorded; every number
printed comes from the commands just executed.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from warehouse.store import ObjectStore


def execute(*arguments, expect_failure=False):
    command = [sys.executable, '-m', 'warehouse', *arguments]
    print('$ ' + ' '.join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    output = result.stdout.strip() or result.stderr.strip()
    print(output, flush=True)
    if expect_failure:
        if result.returncode != 0:
            return None
        raise RuntimeError('预期失败的命令成功了')
    if result.returncode != 0:
        raise RuntimeError(output)
    return json.loads(result.stdout)


def latest_definition(database):
    versions = execute('versions', '--db', database)['versions']
    return versions[-1]['definition'], versions[-1]['version'], versions[-1]['schema_id']


def rename(definition, path, new_name):
    result = json.loads(json.dumps(definition))
    node = result['measurements']
    for name in path[:-1]:
        node = next(field for field in node['fields'] if field['name'] == name)['nested']
    next(field for field in node['fields'] if field['name'] == path[-1])['name'] = new_name
    return result


def main():
    with tempfile.TemporaryDirectory() as temporary:
        database = str(Path(temporary) / 'warehouse.sqlite3')
        objects = str(Path(temporary) / 'objects')
        definition_path = str(Path(temporary) / 'definition.json')
        broken_path = str(Path(temporary) / 'broken.json')

        imported = execute('import', '--db', database,
                           '--input', str(ROOT / 'examples/source.json'), '--batch', 'daily')
        if not imported['changed']:
            raise RuntimeError('首次导入没有写入')
        repeated = execute('import', '--db', database,
                           '--input', str(ROOT / 'examples/source.json'), '--batch', 'daily')
        if repeated['changed']:
            raise RuntimeError('重复导入发生额外写入')

        calculated = execute('inspect', '--db', database)
        exported = execute('export', '--db', database, '--objects', objects)
        restored = ObjectStore(objects).read(exported['object'])
        expected = calculated if isinstance(calculated, list) else [calculated]
        if restored != expected:
            raise RuntimeError('导出与查询结果不符')

        definition, version, schema_id = latest_definition(database)
        evolved = rename(definition, ('temperature',), 'reading')
        Path(definition_path).write_text(json.dumps(evolved, ensure_ascii=False), encoding='utf-8')
        registered = execute('register', '--db', database, '--input', definition_path,
                             '--actor', 'demo', '--note', 'rename temperature to reading',
                             '--base-version', str(version), '--base-schema-id', schema_id)
        print(json.dumps({'registered_version': registered['version']}), flush=True)

        legacy_path = str(Path(temporary) / 'legacy.json')
        legacy = json.loads((ROOT / 'examples/source.json').read_text(encoding='utf-8'))
        for row in legacy['measurements']:
            row['sample'] = 'legacy-' + row['sample']
        Path(legacy_path).write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
        old_writer = execute('import', '--db', database, '--writer-version', '1',
                             '--input', legacy_path, '--batch', 'old-writer')
        if not old_writer['changed']:
            raise RuntimeError('旧版本写者没有写入')

        _, version, _ = latest_definition(database)
        broken = rename(evolved, ('reading',), 'reading')
        broken['measurements']['fields'][2]['nullable'] = False
        Path(broken_path).write_text(json.dumps(broken, ensure_ascii=False), encoding='utf-8')
        execute('register', '--db', database, '--input', broken_path,
                '--actor', 'demo', '--note', 'make reading required', '--base-version', str(version),
                expect_failure=True)

        view = execute('view', '--db', database)
        print(json.dumps({
            'version': view['version'],
            'batches': [{'batch': item['batch'], 'written_version': item['written_version']}
                        for item in view['batches']],
            'operations': [item['kind'] for item in view['operations']],
            'audit': [{'version': item['version'], 'kind': item['kind']}
                      for item in view['audit']],
        }, ensure_ascii=False, sort_keys=True), flush=True)
        print('对象校验通过，重复导入未改变业务结果，改名后旧版本文件仍可投影，收窄被拒绝', flush=True)


if __name__ == '__main__':
    main()
