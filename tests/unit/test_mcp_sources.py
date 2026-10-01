"""tests/unit/test_mcp_sources.py

Tests for the MCP source tools (dango/cli/commands/mcp_sources.py) against a real project on disk.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import load_config, save_config

CSV = "id,amount\n1,10\n2,20\n3,30\n"


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    return root


@pytest.fixture
def csv_file(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "orders.csv"
    path.parent.mkdir()
    path.write_text(CSV)
    return path


def _bare(root: Path, name: str, source_type: str = "local_files") -> None:
    from dango.config.models import DataSource

    cfg = load_config(root)
    cfg.sources.sources.append(DataSource(name=name, type=source_type))
    save_config(cfg, root)


@pytest.mark.unit
def test_list_source_types_names_and_setup_supported() -> None:
    result = mcp_sources.list_source_types()
    stripe = next(r for r in result if r["type"] == "stripe")
    assert stripe["name"] == "Stripe"
    assert stripe["setup_supported"] is True
    assert all("setup_supported" in r for r in result)


@pytest.mark.unit
def test_schema_unknown_type_error(project: Path) -> None:
    assert "error" in mcp_sources.get_source_setup_schema("nope")
    assert mcp_sources.get_source_setup_schema("local_files")["setup_supported"] is True


@pytest.mark.unit
def test_create_local_files_with_file_path_copies_and_syncable(
    project: Path, csv_file: Path
) -> None:
    r = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert r["status"] == "created", r
    copied = project / "data/uploads/orders/orders.csv"
    assert copied.read_text() == CSV
    assert "data/uploads/orders/orders.csv" in r["files_changed"]
    src = load_config(project).sources.get_source("orders")
    assert src is not None and src.local_files is not None

    import duckdb

    from dango.ingestion.dlt_runner import run_sync

    summary = run_sync(project, [src], skip_dbt=True)
    assert summary.get("failed", 0) == 0, summary
    db = next(project.rglob("*.duckdb"))
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from raw_orders.orders").fetchone()[0] == 3
    finally:
        con.close()
    assert mcp_sources.validate_source("orders")["ready"] is True


@pytest.mark.unit
def test_create_file_path_rejects_non_local_files_and_bad_extension_and_pattern_mismatch(
    project: Path, csv_file: Path, tmp_path: Path
) -> None:
    r = mcp_sources.create_source("stripe", "billing", file_path=str(csv_file))
    assert "only supported for local_files" in r["error"]
    txt = tmp_path / "outside" / "notes.txt"
    txt.write_text("x")
    assert (
        "Unsupported file type"
        in mcp_sources.create_source("local_files", "a", file_path=str(txt))["error"]
    )
    r = mcp_sources.create_source(
        "local_files", "b", config={"file_pattern": "data_*.csv"}, file_path=str(csv_file)
    )
    assert "does not match file_pattern" in r["error"]
    assert (
        "does not exist"
        in mcp_sources.create_source("local_files", "c", file_path=str(tmp_path / "missing.csv"))[
            "error"
        ]
    )
    assert load_config(project).sources.sources == []
    assert not (project / "data/uploads/b").exists()


@pytest.mark.unit
def test_create_file_path_never_overwrites_different_file(project: Path, csv_file: Path) -> None:
    existing = project / "data/uploads/orders/orders.csv"
    existing.parent.mkdir(parents=True)
    existing.write_text("different\n")
    r = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert "different file already exists" in r["error"]
    assert existing.read_text() == "different\n"
    # identical bytes: no copy needed, create succeeds
    existing.write_text(CSV)
    assert (
        mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))["status"]
        == "created"
    )


@pytest.mark.unit
def test_two_sources_with_file_path_use_separate_directories(project: Path, csv_file: Path) -> None:
    assert mcp_sources.create_source("local_files", "one", file_path=str(csv_file))["status"] == (
        "created"
    )
    assert mcp_sources.create_source("local_files", "two", file_path=str(csv_file))["status"] == (
        "created"
    )
    assert (project / "data/uploads/one/orders.csv").exists()
    assert (project / "data/uploads/two/orders.csv").exists()


@pytest.mark.unit
def test_create_failure_removes_copied_file(
    project: Path, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.ingestion.sources import setup_service
    from dango.ingestion.sources.setup_schema import SourceSetupError

    def boom(*a: Any, **k: Any) -> None:
        raise SourceSetupError(["boom"])

    monkeypatch.setattr(setup_service, "apply_source", boom)
    r = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert r["errors"] == ["boom"]
    assert not (project / "data/uploads/orders").exists()
    assert csv_file.exists()


@pytest.mark.unit
def test_create_stripe_reports_env_var_requirement_and_next_steps(project: Path) -> None:
    r = mcp_sources.create_source("stripe", "billing")
    assert r["status"] == "created", r
    env = [c for c in r["credentials_required"] if c["kind"] == "env_var"]
    assert env and env[0]["name"]
    assert any(f"set {env[0]['name']} in .env" in s for s in r["next_steps"])
    assert "validate_source('billing')" in r["next_steps"][-1]


@pytest.mark.unit
def test_create_google_sheets_reports_oauth_command(project: Path) -> None:
    r = mcp_sources.create_source(
        "google_sheets",
        "sheet",
        config={"spreadsheet_url_or_id": "abc123", "range_names": ["Sheet1"]},
    )
    assert r["status"] == "created", r
    assert any(c["kind"] == "oauth" for c in r["credentials_required"])
    assert any("dango oauth google_sheets" in s for s in r["next_steps"])


@pytest.mark.unit
def test_create_rejects_secret_value_without_echo(project: Path) -> None:
    secret = "sk_live_SUPERSECRET123"
    r = mcp_sources.create_source("stripe", "billing", config={"stripe_secret_key_env": secret})
    assert "error" in r
    assert secret not in str(r)
    assert load_config(project).sources.get_source("billing") is None


@pytest.mark.unit
def test_update_and_enable_and_remove_flow(project: Path, csv_file: Path) -> None:
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    u = mcp_sources.update_source("orders", config={"file_pattern": "*.csv"})
    assert u["status"] == "updated", u
    src = load_config(project).sources.get_source("orders")
    assert src is not None and src.local_files.file_pattern == "*.csv"
    assert mcp_sources.set_source_enabled("orders", False)["status"] == "disabled"
    assert mcp_sources.set_source_enabled("orders", False)["status"] == "unchanged"
    dry = mcp_sources.remove_source("orders", dry_run=True)
    assert dry["status"] == "dry_run"
    assert load_config(project).sources.get_source("orders") is not None
    assert mcp_sources.remove_source("orders")["status"] == "removed"
    assert load_config(project).sources.get_source("orders") is None
    assert "error" in mcp_sources.remove_source("orders")


@pytest.mark.unit
def test_remove_requires_force_with_downstream(project: Path, csv_file: Path) -> None:
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    model = project / "dbt/models/marts/rev.sql"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_text("select * from {{ ref('stg_orders__orders') }}")
    assert mcp_sources.remove_source("orders", dry_run=True)["downstream_models"] == ["marts.rev"]
    r = mcp_sources.remove_source("orders")
    assert "Models depend" in r["error"]
    assert mcp_sources.remove_source("orders", force=True)["status"] == "removed"


@pytest.mark.unit
def test_validate_source_reports_missing_config_and_no_files(project: Path) -> None:
    _bare(project, "legacy")
    r = mcp_sources.validate_source("legacy")
    assert r["ready"] is False
    assert any("call update_source" in i for i in r["issues"])
    assert "error" in mcp_sources.validate_source("ghost")


@pytest.mark.unit
def test_validate_source_no_files_issue(project: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text(CSV)
    mcp_sources.create_source("local_files", "orders", file_path=str(empty))
    (project / "data/uploads/orders/empty.csv").unlink()
    r = mcp_sources.validate_source("orders")
    assert r["files_found"] == 0
    assert any("No files matching" in i for i in r["issues"])


@pytest.mark.unit
def test_validate_source_ready_when_configured_with_files(project: Path, csv_file: Path) -> None:
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    r = mcp_sources.validate_source("orders")
    assert r == {
        "source_name": "orders",
        "type": "local_files",
        "enabled": True,
        "ready": True,
        "issues": [],
        "files_found": 1,
        "checks": {
            "configuration": True,
            "credentials_present": True,
            "connectivity": "not_checked",
        },
    }


@pytest.mark.unit
def test_validate_source_credentials_cleared_when_env_set(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = mcp_sources.create_source("stripe", "billing")
    name = next(c["name"] for c in r["credentials_required"] if c["kind"] == "env_var")
    assert mcp_sources.validate_source("billing")["ready"] is False
    monkeypatch.setenv(name, "x")
    assert mcp_sources.validate_source("billing")["ready"] is True


@pytest.mark.unit
def test_project_yml_unchanged_after_create(project: Path, csv_file: Path) -> None:
    before = (project / ".dango/project.yml").read_bytes()
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    mcp_sources.create_source("stripe", "billing")
    assert (project / ".dango/project.yml").read_bytes() == before


@pytest.mark.unit
def test_git_warning_on_success_only(
    project: Path, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: ["on main"])
    ok = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert ok["git_warning"] == ["on main"]
    assert "git_warning" not in mcp_sources.create_source("local_files", "orders")
    assert "git_warning" not in mcp_sources.remove_source("orders", dry_run=True)
    assert mcp_sources.remove_source("orders")["git_warning"] == ["on main"]
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    assert "git_warning" not in mcp_sources.create_source("local_files", "again")


@pytest.mark.unit
def test_source_tools_registered() -> None:
    from dango.cli.commands import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {
        "list_source_types",
        "get_source_setup_schema",
        "create_source",
        "update_source",
        "set_source_enabled",
        "remove_source",
        "validate_source",
    } <= names
    assert "add_source" not in names


@pytest.mark.unit
def test_partial_copy_failure_is_cleaned_up(
    project: Path, csv_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def half_copy(src: Any, dest: Any) -> None:
        Path(dest).write_text("par")
        raise OSError("disk full")

    monkeypatch.setattr(mcp_sources.shutil, "copy2", half_copy)
    r = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert "error" in r
    assert not (project / "data/uploads/orders").exists()


@pytest.mark.unit
def test_create_refuses_symlink_destination(project: Path, csv_file: Path, tmp_path: Path) -> None:
    d = project / "data/uploads/orders"
    d.mkdir(parents=True)
    (d / "orders.csv").symlink_to(tmp_path / "dangling-target.csv")
    r = mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert "symlink" in r["error"]
    assert not (tmp_path / "dangling-target.csv").exists()


@pytest.mark.unit
def test_validate_source_ignores_dotfiles_like_the_loader(project: Path, csv_file: Path) -> None:
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    d = project / "data/uploads/orders"
    (d / "orders.csv").rename(d / ".hidden.csv")
    r = mcp_sources.validate_source("orders")
    assert r["files_found"] == 0 and r["ready"] is False


@pytest.mark.unit
def test_setup_schema_csv_points_to_local_files() -> None:
    r = mcp_sources.get_source_setup_schema("csv")
    assert r["use_instead"] == "local_files"
    assert "legacy type" in r["note"] and "file_path" in r["note"]
    assert "use_instead" not in mcp_sources.get_source_setup_schema("local_files")


@pytest.mark.unit
def test_create_source_csv_rejected_with_hint_no_writes(project: Path, csv_file: Path) -> None:
    before = (project / ".dango" / "sources.yml").read_bytes()
    for kwargs in ({}, {"file_path": str(csv_file)}):
        r = mcp_sources.create_source("csv", "orders", **kwargs)
        assert "local_files" in r["error"] and r["errors"] == [r["error"]]
        assert "status" not in r
    assert (project / ".dango" / "sources.yml").read_bytes() == before
    assert not (project / "data").exists()
    assert load_config(project).sources.get_source("orders") is None


@pytest.mark.unit
def test_existing_csv_source_still_validates(project: Path) -> None:
    from dango.config.models import CSVSourceConfig, DataSource

    (project / "data" / "legacy").mkdir(parents=True)
    (project / "data" / "legacy" / "a.csv").write_text(CSV)
    cfg = load_config(project)
    cfg.sources.sources.append(
        DataSource(name="legacy", type="csv", csv=CSVSourceConfig(directory="data/legacy"))
    )
    save_config(cfg, project)
    r = mcp_sources.validate_source("legacy")
    assert r["ready"] is True and r["files_found"] == 1, r
    assert mcp_sources.set_source_enabled("legacy", False).get("error") is None
    assert mcp_sources.remove_source("legacy").get("error") is None


@pytest.mark.unit
def test_create_source_file_path_ignores_matching_directory(project: Path, csv_file: Path) -> None:
    r = mcp_sources.create_source(
        "local_files",
        "orders",
        config={"directory": "data/uploads/orders"},
        file_path=str(csv_file),
    )
    assert r["status"] == "created", r
    assert (project / "data/uploads/orders/orders.csv").read_text() == CSV


@pytest.mark.unit
def test_create_source_file_path_normalizes_directory(project: Path, csv_file: Path) -> None:
    r = mcp_sources.create_source(
        "local_files",
        "orders",
        config={"directory": "./data/uploads/orders/"},
        file_path=str(csv_file),
    )
    assert r["status"] == "created", r
    bad = mcp_sources.create_source(
        "local_files", "o2", config={"directory": "/data/uploads/o2"}, file_path=str(csv_file)
    )
    assert "omit 'directory'" in bad["error"]


@pytest.mark.unit
def test_create_source_file_path_rejects_conflicting_directory(
    project: Path, csv_file: Path
) -> None:
    r = mcp_sources.create_source(
        "local_files", "orders", config={"directory": "data/uploads"}, file_path=str(csv_file)
    )
    assert "omit 'directory'" in r["error"] and "data/uploads/orders/" in r["error"]
    assert not (project / "data").exists()
    assert load_config(project).sources.get_source("orders") is None


@pytest.mark.unit
def test_validate_source_checks_key_and_docstring_mentions_oauth_only(
    project: Path, csv_file: Path
) -> None:
    mcp_sources.create_source("local_files", "orders", file_path=str(csv_file))
    assert mcp_sources.validate_source("orders")["checks"]["connectivity"] == "not_checked"
    r = mcp_sources.validate_source("orders", check_connectivity=True)
    assert r["checks"] == {
        "configuration": True,
        "credentials_present": True,
        "connectivity": "oauth_only",
    }
    doc = " ".join((mcp_sources.validate_source.__doc__ or "").split())
    assert "only validates OAuth tokens" in doc and "does not prove an API key" in doc
    _bare(project, "empty")
    bad = mcp_sources.validate_source("empty")
    assert bad["checks"]["configuration"] is False


@pytest.mark.unit
def test_validate_source_checks_credentials_missing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    created = mcp_sources.create_source("stripe", "billing", config={"start_date": "2024-01-01"})
    assert created["credentials_required"], created
    r = mcp_sources.validate_source("billing")
    assert r["ready"] is False
    assert r["checks"]["credentials_present"] is False
