"""The registered baseline for this warehouse.

Version 1 mirrors the pre-registry flat structure: a ``measurements`` row had
``sample``, ``device``, ``temperature`` and ``unit`` text columns.  Its
identities are derived deterministically so an existing database upgrades
without guessing which old column is which.
"""

from .schema import derive_field_id


def _field(table, name, field_type, nullable, **extra):
    return {
        'name': name,
        'field_id': derive_field_id(table, (name,)),
        'type': field_type,
        'nullable': nullable,
        'nested': None,
        **extra,
    }


def initial_definition():
    """Return a fresh mutable copy of the version-1 definition."""
    sample = _field('measurements', 'sample', 'string', False)
    device = _field('measurements', 'device', 'string', False)
    temperature = _field('measurements', 'temperature', 'string', True)
    unit = _field('measurements', 'unit', 'string', False)
    return {
        'measurements': {
            'key': [sample['field_id']],
            'fields': [sample, device, temperature, unit],
        }
    }
