"""tests/integration/t44a_qualification/semantic_oracles.py

Fixed source rows and independent Python arithmetic/IANA calendar expectations for N1.
"""

import datetime as dt
from collections import defaultdict
from zoneinfo import ZoneInfo

# id, customer, amount, units, category, active, actual aware event instant.
ROWS = [
    (1, "a", 100, 2, "A", True, "2026-08-31T16:00:00+00:00"),
    (2, "a", 200, 4, "A", True, "2026-09-30T16:00:00+00:00"),
    (3, "b", 50, 1, "B", False, "2026-10-01T00:00:00+00:00"),
    (4, "c", 0, 0, "zero", True, "2026-03-08T06:59:59+00:00"),
    (5, "c", 0, 0, "zero", False, "2026-03-08T07:00:00+00:00"),
    (6, None, None, None, "null", None, "2026-11-01T05:30:00+00:00"),
    (7, "d", 30, 3, "C", True, "2026-11-01T06:30:00+00:00"),
    (8, "d", 70, 7, "C", False, "2024-02-29T23:30:00+00:00"),
]
CUSTOMERS = [("a", "retail"), ("b", "retail"), ("c", "wholesale"), ("d", "wholesale")]


def independent_cases():
    """Use Python arithmetic/sets/IANA calendars, independently of generated native SQL."""
    amounts = [r[2] for r in ROWS if r[2] is not None]
    units = [r[3] for r in ROWS if r[3] is not None]
    cases = []

    def add(name, metrics, expected, dimensions=(), filters=()):
        cases.append(
            {
                "name": name,
                "metrics": list(metrics),
                "expected": expected,
                "dimensions": list(dimensions),
                "filters": list(filters),
            }
        )

    add(
        "aggregates",
        [
            "total_amount",
            "row_count",
            "amount_count",
            "min_amount",
            "max_amount",
            "average_amount",
            "customers",
            "ratio",
        ],
        [
            [
                sum(amounts),
                len(ROWS),
                len(amounts),
                min(amounts),
                max(amounts),
                sum(amounts) / len(amounts),
                len({r[1] for r in ROWS if r[1] is not None}),
                sum(amounts) / sum(units),
            ]
        ],
    )
    for category in ["zero", "null"]:
        add(
            category + "_ratio",
            ["total_amount", "total_units", "ratio"],
            [[0, 0, None]] if category == "zero" else [[None, None, None]],
            filters=[("category", "equals", [category])],
        )
    add("categorical_filter", ["total_amount"], [[300]], filters=[("category", "equals", ["A"])])
    add(
        "numeric_filter",
        ["total_amount"],
        [[300]],
        filters=[("amount", "greaterThanOrEqual", [100])],
    )
    add("boolean_filter", ["total_amount"], [[330]], filters=[("active", "equals", [True])])
    add(
        "null_filter",
        ["row_count", "total_amount"],
        [[1, None]],
        filters=[("amount", "isNull", [])],
    )
    # Half-open, typed DATE filters execute before aggregation; UTC Sep has only id2.
    add(
        "typed_date_filter",
        ["total_amount"],
        [[200]],
        filters=[
            ("utc_date_day", "greaterThanOrEqual", ["2026-09-01"]),
            ("utc_date_day", "lessThan", ["2026-10-01"]),
        ],
    )
    for label, zone in [
        ("utc", "UTC"),
        ("singapore", "Asia/Singapore"),
        ("new_york", "America/New_York"),
    ]:
        grouped = defaultdict(list)
        for row in ROWS:
            local = dt.datetime.fromisoformat(row[6]).astimezone(ZoneInfo(zone))
            grouped[local.date().isoformat()].append(row)
        expected = []
        for key, rows in sorted(grouped.items()):
            values = [r[2] for r in rows if r[2] is not None]
            expected.append([key, sum(values) if values else None, len(rows)])
        add(
            label + "_calendar",
            ["total_amount", "row_count"],
            expected,
            dimensions=[label + "_date_day"],
        )
    # Distinguish the missing spring hour and both identical fall wall-clock hours.
    dst = []
    for row in ROWS:
        if row[0] not in {4, 5, 6, 7}:
            continue
        local = dt.datetime.fromisoformat(row[6]).astimezone(ZoneInfo("America/New_York"))
        dst.append(
            [row[0], local.date().isoformat(), local.hour, int(local.utcoffset().total_seconds())]
        )
    add(
        "dst_gap_and_fold",
        [],
        dst,
        dimensions=["id", "new_york_date_day", "new_york_hour", "new_york_offset"],
        filters=[("id", "greaterThanOrEqual", [4]), ("id", "lessThan", [8])],
    )
    # Customer a spans two days: requested-grain distinct must not sum daily counts.
    daily = defaultdict(set)
    for row in ROWS:
        if row[1] is not None:
            daily[row[6][:10]].add(row[1])
    add(
        "daily_distinct_not_reaggregatable",
        ["customers"],
        [[day, len(values)] for day, values in sorted(daily.items())],
        dimensions=["utc_date_day"],
    )
    segments = dict(CUSTOMERS)
    grouped = defaultdict(list)
    for row in ROWS:
        grouped[segments.get(row[1])].append(row)
    expected = []
    for segment, rows in grouped.items():
        values = [r[2] for r in rows if r[2] is not None]
        expected.append([segment, sum(values) if values else None, len(rows)])
    add(
        "many_to_one_join",
        ["total_amount", "row_count"],
        expected,
        dimensions=["semantic_customers.segment"],
    )
    add(
        "preaggregated_lines", ["total_amount", "semantic_line_totals.total_line_value"], [[450, 6]]
    )
    return cases
