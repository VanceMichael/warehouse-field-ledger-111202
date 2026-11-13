"""Cold-process probe: prove projection relies on persisted registry state.

The subprocess is invoked with ``python3 -m tests.fixtures.projection_probe``
from the repository root, so no in-process state from the test is available.
Phase ``prepare`` writes an old-version file and registers a rename; phase
``read`` starts a fresh interpreter and projects that file into the renamed
shape.
"""

import json
import sys
from pathlib import Path

from warehouse.catalog import connect, load_bundle, register
from warehouse.domain import overview
from warehouse.registry import Registry
from warehouse.schema import rename_field

ROOT = Path(__file__).resolve().parents[2]


def prepare(database):
    connection = connect(database)
    try:
        registry = Registry(connection)
        bundle = json.loads((ROOT / 'examples/source.json').read_text(encoding='utf-8'))
        load_bundle(connection, bundle, 'probe')
        record = register(
            connection,
            rename_field(registry.current()['definition'],
                         'measurements', ('temperature',), 'reading'),
            actor='probe',
        )
        return {'phase': 'prepare', 'version': record['version']}
    finally:
        connection.close()


def read(database):
    connection = connect(database)
    try:
        return overview(connection)
    finally:
        connection.close()


def main(argv):
    if len(argv) != 3:
        raise SystemExit('usage: projection_probe.py <prepare|read> <database>')
    phase, database = argv[1], argv[2]
    result = prepare(database) if phase == 'prepare' else read(database)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
