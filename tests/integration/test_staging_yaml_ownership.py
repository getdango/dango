"""tests/integration/test_staging_yaml_ownership.py

Post-sync enrichment changes a generated staging schema file only while its bytes are still
what Dango last wrote (SHA-256 sidecar under .dango/state); a user-edited file is never touched.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest
import yaml

from dango.config.models import DataSource, SourceType
from dango.transformation.generator import DbtModelGenerator
from dango.utils.post_sync import _enrich_staging_tests, _run_profiling

pytestmark = pytest.mark.integration

SOURCE = "shop"
REL = "dbt/models/staging/stg_shop.yml"


def _make_project(tmp_path: Path) -> Path:
    """Two raw tables; `note` and `size` each contain a blank, everything else is complete."""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "dbt" / "models" / "staging").mkdir(parents=True)
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA raw_shop")
    conn.execute("CREATE TABLE raw_shop.orders (id INTEGER, status VARCHAR, note VARCHAR)")
    conn.execute("INSERT INTO raw_shop.orders VALUES (1, 'a', 'x'), (2, 'b', NULL)")
    conn.execute("CREATE TABLE raw_shop.items (id INTEGER, sku VARCHAR, size VARCHAR)")
    conn.execute("INSERT INTO raw_shop.items VALUES (1, 'p', 'm'), (2, 'q', NULL)")
    conn.close()
    return root


def _generate(root: Path) -> None:
    source = DataSource(name=SOURCE, type=SourceType.STRIPE)
    assert DbtModelGenerator(root).generate_all_models([source])["errors"] == []


def _sync(root: Path) -> None:
    _generate(root)
    _run_profiling(root, [SOURCE])
    _enrich_staging_tests(root, [SOURCE])


def _fill_blanks(root: Path) -> None:
    """A later sync in which the blank cells are filled: `note` and `size` now qualify."""
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    conn.execute("UPDATE raw_shop.orders SET note = 'y' WHERE note IS NULL")
    conn.execute("UPDATE raw_shop.items SET size = 's' WHERE size IS NULL")
    conn.close()


def _schema(root: Path) -> Path:
    return root / REL


def _sidecar(root: Path) -> Path:
    return root / ".dango" / "state" / "staging_yaml_hashes.json"


def _recorded(root: Path) -> str | None:
    if not _sidecar(root).exists():
        return None
    data = json.loads(_sidecar(root).read_text(encoding="utf-8"))
    return data["files"].get(REL)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _columns(root: Path, model: str) -> dict[str, list]:
    data = yaml.safe_load(_schema(root).read_text(encoding="utf-8"))
    by_model = {m["name"]: m for m in data["models"]}
    return {c["name"]: c.get("tests", []) for c in by_model[model]["columns"]}


def _edit(root: Path, *, append: str = "", crlf: bool = False) -> None:
    """Simulate a user edit of the schema file."""
    text = _schema(root).read_text(encoding="utf-8") + append
    if crlf:
        text = text.replace("\n", "\r\n")
    _schema(root).write_bytes(text.encode("utf-8"))


def _resync_after_fill(root: Path) -> bytes:
    _fill_blanks(root)
    _run_profiling(root, [SOURCE])
    _enrich_staging_tests(root, [SOURCE])
    return _schema(root).read_bytes()


def test_sidecar_records_the_bytes_dango_last_wrote(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _generate(root)
    assert _recorded(root) == _sha(_schema(root))
    _run_profiling(root, [SOURCE])
    _enrich_staging_tests(root, [SOURCE])
    assert "not_null" in json.dumps(_columns(root, "stg_shop__orders"))
    assert _recorded(root) == _sha(_schema(root))


def test_unedited_file_keeps_being_enriched_after_an_earlier_enrichment(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    assert _columns(root, "stg_shop__orders")["note"] == []
    _resync_after_fill(root)
    assert _columns(root, "stg_shop__orders")["note"] != []
    assert _columns(root, "stg_shop__items")["size"] != []
    assert _recorded(root) == _sha(_schema(root))


def test_deleted_check_does_not_come_back(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    data = yaml.safe_load(_schema(root).read_text(encoding="utf-8"))
    orders = next(m for m in data["models"] if m["name"] == "stg_shop__orders")
    for col in orders["columns"]:
        if col["name"] == "status":
            assert col["tests"]
            del col["tests"]  # the user decides status needs no check
    _schema(root).write_text(
        yaml.dump(data, sort_keys=False) + "# status may be blank on purpose\n",
        encoding="utf-8",
        newline="\n",
    )
    before = _schema(root).read_bytes()
    assert _resync_after_fill(root) == before
    assert _columns(root, "stg_shop__orders")["status"] == []


@pytest.mark.parametrize(
    "edit",
    [
        {"append": "\n# my notes\n"},
        {"crlf": True},
    ],
    ids=["comment-added", "line-endings-changed"],
)
def test_edited_file_is_never_touched(tmp_path: Path, edit: dict) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    _edit(root, **edit)
    before = _schema(root).read_bytes()
    assert _resync_after_fill(root) == before


def test_edit_in_another_models_block_protects_the_whole_file(tmp_path: Path) -> None:
    """Two models in one file: a comment on one, the other would gain a check."""
    root = _make_project(tmp_path)
    _sync(root)
    text = _schema(root).read_text(encoding="utf-8")
    marker = "\n- name: stg_shop__items"
    assert marker in text
    _schema(root).write_text(
        text.replace(marker, "\n# items is mine now" + marker, 1), encoding="utf-8", newline="\n"
    )
    sql = _schema(root).parent / "stg_shop__items.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8").replace("-- Auto-generated by Dango", "-- Mine"),
        encoding="utf-8",
        newline="\n",
    )
    before = _schema(root).read_bytes()
    assert _resync_after_fill(root) == before


def test_file_with_no_recorded_hash_is_never_rewritten(tmp_path: Path) -> None:
    """A project from an earlier Dango version has no sidecar entry."""
    root = _make_project(tmp_path)
    _generate(root)
    _sidecar(root).unlink(missing_ok=True)
    _run_profiling(root, [SOURCE])
    before = _schema(root).read_bytes()
    _enrich_staging_tests(root, [SOURCE])
    assert _schema(root).read_bytes() == before
    assert _recorded(root) is None


def test_corrupt_sidecar_means_never_rewritten(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _generate(root)
    _sidecar(root).parent.mkdir(parents=True, exist_ok=True)
    _sidecar(root).write_text("{not json", encoding="utf-8", newline="\n")
    _run_profiling(root, [SOURCE])
    before = _schema(root).read_bytes()
    _enrich_staging_tests(root, [SOURCE])
    assert _schema(root).read_bytes() == before


def test_regenerated_file_is_recorded_and_enriched_again(tmp_path: Path) -> None:
    """Deleting the file (the documented way back to Dango's checks) starts a fresh record."""
    root = _make_project(tmp_path)
    _sync(root)
    _edit(root, append="# edited\n")
    _schema(root).unlink()
    _sync(root)
    assert _recorded(root) == _sha(_schema(root))
    assert "# edited" not in _schema(root).read_text(encoding="utf-8")
    assert _columns(root, "stg_shop__orders")["status"] != []


def test_sidecar_is_utf8_json_with_lf_endings(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    assert _sidecar(root).exists()
    raw = _sidecar(root).read_bytes()
    assert b"\r" not in raw
    assert json.loads(raw.decode("utf-8"))["files"][REL] == _sha(_schema(root))
    assert not list(_sidecar(root).parent.glob("*.tmp"))


def test_header_states_what_is_true(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _generate(root)
    header = "".join(
        ln for ln in _schema(root).read_text(encoding="utf-8").splitlines(True) if ln[0] == "#"
    )
    assert "will not be overwritten if you customize it" not in header
    for word in ("id", "uuid", "key", "NOT NULL", "severity: warn", "have not edited"):
        assert word in header


@pytest.mark.parametrize(
    "new_sql",
    [
        "-- Mine\nselect id, sku from {{ source('shop', 'items') }}\n",
        "-- Mine\nselect id, sku, size as item_size from {{ source('shop', 'items') }}\n",
    ],
    ids=["column-dropped", "column-renamed"],
)
def test_customized_model_gets_no_check_while_the_unedited_file_is_enriched(
    tmp_path: Path, new_sql: str
) -> None:
    """The file is still Dango's, but a model whose SQL the user took over gains no check."""
    root = _make_project(tmp_path)
    _sync(root)
    (_schema(root).parent / "stg_shop__items.sql").write_text(
        new_sql, encoding="utf-8", newline="\n"
    )
    items_before = _columns(root, "stg_shop__items")
    _resync_after_fill(root)
    assert _columns(root, "stg_shop__orders")["note"] != []  # the file itself was enriched
    assert _columns(root, "stg_shop__items") == items_before
    assert _columns(root, "stg_shop__items")["size"] == []
