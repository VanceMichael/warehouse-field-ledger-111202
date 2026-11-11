from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT sample, device, temperature, unit FROM measurements ORDER BY sample')]
    return rows
