"""tests/unit/test_input_copy_sql.py

Prove structured copy statements preserve hostile-looking identifiers, values and constraints.
"""

from pathlib import Path

import duckdb

from dango.storage.input_copy import (
    NativeInputCopy,
    _create_sql,
    _describe_sql,
    _export_sql,
    _insert_sql,
)
from dango.storage.input_state import Column, InputCaptureError, Relation, fingerprint


def exercise_sql_roundtrip(connection, root):
    """Use independent literal native names/rows/schema answers; also reused by native case04."""
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection.execute('CREATE SCHEMA catalog."src""; DROP TABLE sentinel; --"')
    connection.execute('CREATE SCHEMA catalog."dst space"')
    connection.execute('CREATE TABLE catalog."dst space".sentinel(value INTEGER)')
    connection.execute('INSERT INTO catalog."dst space".sentinel VALUES (99)')
    columns = (
        Column(name='id"; DROP TABLE sentinel; --', type="INTEGER", nullable=False),
        Column(name="amount space", type="BIGINT", nullable=True),
    )
    source = Relation(
        relation_id="sql-source",
        schema_name='src"; DROP TABLE sentinel; --',
        table_name="orders.dot",
        table_uuid="source-id",
        columns=columns,
        schema_fingerprint=fingerprint(columns),
        source_version_ref="sql-source-v1",
        checkpoint_ref="sql-checkpoint-v1",
    )
    target = source.model_copy(
        update={"schema_name": "dst space", "table_name": "orders'; SELECT 9; --"}
    )
    connection.execute(_create_sql(source))
    connection.execute(
        'INSERT INTO catalog."src""; DROP TABLE sentinel; --"."orders.dot" VALUES (1,7),(2,NULL)'
    )
    path = Path(root) / "literal'; DROP TABLE sentinel; --.parquet"
    connection.execute(_export_sql(source), [str(path)])
    assert path.is_file()
    connection.execute(_create_sql(target))
    connection.execute(_insert_sql(target), [str(path)])
    expected_columns = [
        ('id"; DROP TABLE sentinel; --', "INTEGER", "NO"),
        ("amount space", "BIGINT", "YES"),
    ]
    assert [
        row[:3] for row in connection.execute(_describe_sql(target)).fetchall()
    ] == expected_columns
    observed = connection.execute(
        "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
        "WHERE table_catalog='catalog' AND table_schema=? AND table_name=? ORDER BY ordinal_position",
        ["dst space", "orders'; SELECT 9; --"],
    ).fetchall()
    assert observed == expected_columns
    select = 'SELECT * FROM catalog."dst space"."orders\'; SELECT 9; --" ORDER BY 1'
    assert connection.execute(select).fetchall() == [(1, 7), (2, None)]
    try:
        connection.execute(
            'INSERT INTO catalog."dst space"."orders\'; SELECT 9; --" VALUES(NULL,7)'
        )
    except duckdb.ConstraintException:
        pass
    else:
        raise AssertionError("NOT NULL constraint was lost")
    try:
        connection.execute(_create_sql(target))
    except duckdb.CatalogException:
        pass
    else:
        raise AssertionError("Existing target was overwritten")
    assert connection.execute(select).fetchall() == [(1, 7), (2, None)]
    assert connection.execute('SELECT * FROM catalog."dst space".sentinel').fetchall() == [(99,)]
    try:
        NativeInputCopy._collisions(connection, (target,))
    except InputCaptureError as error:
        assert error.code == "capture_collision"
    else:
        raise AssertionError("Preflight collision was admitted")
    invalid = source.model_copy(
        update={
            "columns": (
                columns[0].model_copy(update={"type": "INTEGER); DROP TABLE sentinel; --"}),
            )
        }
    )
    try:
        _create_sql(invalid)
    except InputCaptureError as error:
        assert error.code == "capture_schema_changed"
    else:
        raise AssertionError("Unadmitted type became SQL")
    return {
        "adversarial_names_preserved": True,
        "file_value_bound": True,
        "ordered_schema": expected_columns,
        "rows": [(1, 7), (2, None)],
        "null_and_collision_refused": True,
        "sentinel": 99,
    }


def test_copy_sql_preserves_identifiers_values_schema_and_collisions(tmp_path):
    with duckdb.connect() as connection:
        connection.execute("ATTACH ':memory:' AS catalog")
        exercise_sql_roundtrip(connection, tmp_path)
