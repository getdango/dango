"""tests/unit/test_wp20r_followups.py

Private download path for the spaCy wheel, names that reject a trailing newline, honest
`source remove` when a staging file cannot be deleted, and `cleanup --docker` removing exactly
the volumes it listed.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.cli.commands.cleanup import cleanup
from dango.cli.commands.project import rename
from dango.cli.commands.upgrade import _validate_version
from dango.config.helpers import load_config, save_config
from dango.config.models import DataSource
from dango.governance import pii_detector
from dango.platform.docker import get_compose_project_name

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _run_analyzer_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, install_ok: bool):
    """Run `_get_analyzer` with no spaCy model; return (planted file, urlretrieve dest, wheel)."""
    shared = tmp_path / "shared_tmp"
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    planted = shared / pii_detector._SPACY_MODEL_URL.rsplit("/", 1)[-1]
    planted.write_bytes(b"planted by another user")
    monkeypatch.setattr(pii_detector, "_analyzer", None)
    monkeypatch.setattr(pii_detector, "_analyzer_init_failed", False)
    seen: dict[str, Path] = {}

    def retrieve(_url: str, dest: str) -> None:
        seen["dest"] = Path(dest)
        Path(dest).write_bytes(b"wheel")

    def install(argv: list[str], **_kw: object) -> None:
        seen["wheel"] = Path(argv[-1])
        if not install_ok:
            raise OSError("pip failed")

    with (
        patch("spacy.load", side_effect=OSError("no model")),
        patch("urllib.request.urlretrieve", side_effect=retrieve),
        patch("subprocess.check_call", side_effect=install),
    ):
        try:
            pii_detector._get_analyzer()
        except Exception:
            pass  # the model is still absent after the mocked install
    return planted, seen


@pytest.mark.unit
@pytest.mark.parametrize("install_ok", [True, False])
def test_spacy_wheel_never_uses_or_deletes_a_file_at_the_fixed_temp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, install_ok: bool
) -> None:
    planted, seen = _run_analyzer_init(monkeypatch, tmp_path, install_ok)
    assert seen["dest"] != planted
    assert seen["wheel"] == seen["dest"]
    assert planted.read_bytes() == b"planted by another user"
    assert not seen["dest"].exists(), "the downloaded wheel was left behind"


@pytest.mark.unit
def test_rename_rejects_a_name_with_a_trailing_newline(tmp_path: Path) -> None:
    with patch("dango.cli.utils.require_project_context", return_value=tmp_path):
        result = CliRunner().invoke(rename, ["name\n"], obj={"project_root": str(tmp_path)})
    assert "Invalid project name" in _strip(result.output)
    assert result.exit_code != 0


@pytest.mark.unit
def test_upgrade_version_rejects_a_trailing_newline() -> None:
    with pytest.raises(click.BadParameter):
        _validate_version("1.2.3\n")
    assert _validate_version("1.2.3") is None


def _project_with_source(tmp_path: Path, sample_config) -> Path:
    sample_config.sources.sources = [
        DataSource(
            name="orders",
            type="local_files",
            local_files={"directory": "data/uploads/orders", "file_pattern": "*.csv"},
        )
    ]
    save_config(sample_config, tmp_path)
    staging = tmp_path / "dbt" / "models" / "staging"
    staging.mkdir(parents=True)
    for name in ("stg_orders__t.sql", "sources_orders.yml", "stg_orders.yml"):
        (staging / name).write_text("x", encoding="utf-8")
    return tmp_path


def _failing_unlink(stuck: str):
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == stuck:
            raise PermissionError("in use")
        real(self, missing_ok=missing_ok)

    return unlink


@pytest.mark.unit
def test_remove_source_reports_a_staging_file_it_could_not_delete(
    tmp_path: Path, sample_config
) -> None:
    from dango.ingestion.sources.setup_lifecycle import remove_source

    project = _project_with_source(tmp_path, sample_config)
    with patch.object(Path, "unlink", _failing_unlink("sources_orders.yml")):
        result = remove_source(project, "orders")

    staging = project / "dbt" / "models" / "staging"
    assert load_config(project).sources.get_source("orders") is None
    assert (staging / "sources_orders.yml").exists()
    assert not (staging / "stg_orders__t.sql").exists()
    assert not (staging / "stg_orders.yml").exists()
    assert any("sources_orders.yml" in w and "in use" in w for w in result.warnings)
    assert "dbt/models/staging/sources_orders.yml" not in [
        p.replace("\\", "/") for p in result.files_removed
    ]


@pytest.mark.unit
def test_source_remove_cli_does_not_claim_success_when_a_file_stays(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.main import cli

    project = _project_with_source(tmp_path, sample_config)
    monkeypatch.chdir(project)
    monkeypatch.setattr("dango.transformation.generate_dbt_docs", lambda *_a, **_k: None)
    with patch.object(Path, "unlink", _failing_unlink("sources_orders.yml")):
        result = CliRunner().invoke(cli, ["source", "remove", "orders", "--yes"])

    out = _strip(result.output)
    assert "removed successfully" not in out
    assert "sources_orders.yml" in out and "could not be removed" in out
    assert result.exit_code != 0
    assert not (project / "dbt" / "models" / "staging" / "stg_orders.yml").exists()


@pytest.mark.unit
def test_docker_cleanup_removes_exactly_the_volumes_it_listed(tmp_path: Path) -> None:
    prefix = get_compose_project_name(tmp_path)
    listed = f"{prefix}_metabase-data"
    appeared_later = f"{prefix}_appeared-after-confirmation"
    ls_calls: list[int] = []
    removed: list[str] = []

    def run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        if argv[:3] == ["docker", "volume", "ls"]:
            ls_calls.append(1)
            names = [listed] if len(ls_calls) == 1 else [listed, appeared_later]
            return MagicMock(returncode=0, stdout="\n".join(names) + "\n")
        if argv[:3] == ["docker", "volume", "rm"]:
            removed.append(argv[3])
        return MagicMock(returncode=0, stdout="")

    with (
        patch("subprocess.run", side_effect=run),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
    ):
        result = CliRunner().invoke(
            cleanup, ["--docker"], input="y\n", obj={"project_root": str(tmp_path)}
        )

    assert result.exit_code == 0, result.output
    assert len(ls_calls) == 1
    assert removed == [listed]
