"""tests/unit/test_source_key_and_env_checks.py

`dango validate` checks the credential variable a source really uses and warns about
misspelled keys inside a source block (the source still loads).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dango.cli.validate import ProjectValidator
from dango.config import ConfigLoader

pytestmark = pytest.mark.unit


def _local(name: str, directory: str = "data/uploads/a", **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {"directory": directory, **extra}
    return {"name": name, "type": "local_files", "local_files": block}


def _write_project(root: Path, sources: list[dict[str, Any]]) -> Path:
    dango = root / ".dango"
    dango.mkdir(parents=True, exist_ok=True)
    (dango / "project.yml").write_text(
        yaml.safe_dump(
            {"project": {"name": "t", "created_by": "x@example.com", "purpose": "testing"}}
        ),
        encoding="utf-8",
    )
    (dango / "sources.yml").write_text(
        yaml.safe_dump({"version": "1.0", "sources": sources}), encoding="utf-8", newline="\n"
    )
    return root


def _rows(validator: ProjectValidator, prefix: str) -> list[Any]:
    return [r for r in validator.results if r.name.startswith(prefix)]


def _display(validator: ProjectValidator) -> str:
    """What `dango validate` prints for the collected rows (a MarkupError would raise here)."""
    from dango.cli.validate import console

    with console.capture() as captured:
        validator._display_results()
    return " ".join(captured.get().split())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


# ---------------------------------------------------------------------------
# L1: validate checks the variable the source really uses
# ---------------------------------------------------------------------------


def _stripe(name: str, **block: Any) -> dict[str, Any]:
    return {"name": name, "type": "stripe", "stripe": block}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("STRIPE_API_KEY", "PAYMENTS_API_KEY", "PAYMENTS_STRIPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestEnvVarCheckUsesTheSourcesOwnVariable:
    def test_own_variable_set_gives_no_warning(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        monkeypatch.setenv("PAYMENTS_API_KEY", "x")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")

    def test_own_variable_missing_warns_naming_the_real_variable(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        monkeypatch.setenv("STRIPE_API_KEY", "registry-default-is-set")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        rows = _rows(validator, "Env: payments")
        assert len(rows) == 1 and rows[0].status == "warn"
        assert "PAYMENTS_API_KEY" in rows[0].message
        assert "STRIPE_API_KEY" not in rows[0].message

    def test_variable_in_the_project_env_file_counts_as_set(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        (root / ".env").write_text("PAYMENTS_API_KEY=abc\n", encoding="utf-8")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")

    def test_default_variable_name_still_checked_when_it_is_the_one_in_use(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        root = _write_project(tmp_path, [_stripe("stripe", stripe_secret_key_env="STRIPE_API_KEY")])
        validator = ProjectValidator(root)

        validator._check_env_vars()

        rows = _rows(validator, "Env: stripe")
        assert len(rows) == 1 and "STRIPE_API_KEY" in rows[0].message

    def test_file_sources_print_no_env_row(self, tmp_path: Path, clean_env: None) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")


# ---------------------------------------------------------------------------
# L7a: misspelled keys inside a source block warn, and the source still loads
# ---------------------------------------------------------------------------


class TestUnknownKeysInSourceBlocks:
    def test_a_typo_warns_naming_the_source_and_the_key(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales", file_patern="*.txt")])
        validator = ProjectValidator(root)

        validator._check_source_keys()

        rows = _rows(validator, "Source keys: sales")
        assert len(rows) == 1 and rows[0].status == "warn"
        assert "file_patern" in rows[0].message and "local_files" in rows[0].message

    def test_bracket_text_in_a_key_or_a_source_name_is_shown_not_swallowed_or_fatal(
        self, tmp_path: Path
    ) -> None:
        root = _write_project(
            tmp_path,
            [_local("sales", **{"[/]": 1, "filter[status]": 2}), _local("x[/]", **{"k": 3})],
        )
        validator = ProjectValidator(root)
        validator._check_source_keys()

        text = _display(validator)
        assert "local_files.[/]" in text
        assert "local_files.filter[status]" in text
        assert "x[/]" in text

    def test_the_source_with_the_typo_still_loads_with_the_default(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales", file_patern="*.txt")])

        source = ConfigLoader(root).load_sources_config().get_source("sales")

        assert source is not None and source.local_files is not None
        assert source.local_files.file_pattern == "*"

    def test_every_misspelled_key_of_every_source_is_named(self, tmp_path: Path) -> None:
        root = _write_project(
            tmp_path,
            [
                _local("a", file_patern="x", directry="y"),
                _local("b"),
                _stripe("c", stripe_secret_key_envv="K"),
            ],
        )
        validator = ProjectValidator(root)

        validator._check_source_keys()

        names = {r.name for r in _rows(validator, "Source keys")}
        assert names == {"Source keys: a", "Source keys: c"}
        a_row = _rows(validator, "Source keys: a")[0]
        assert "file_patern" in a_row.message and "directry" in a_row.message

    def test_known_keys_and_aliases_do_not_warn(self, tmp_path: Path) -> None:
        root = _write_project(
            tmp_path,
            [
                _local("a", file_pattern="*.csv", notes="n"),
                {"name": "s", "type": "slack", "slack": {"token_env": "T", "channels": ["C1"]}},
                {
                    "name": "n",
                    "type": "dlt_native",
                    "dlt_native": {"source_module": "m", "function_kwargs": {"anything": 1}},
                },
            ],
        )
        validator = ProjectValidator(root)

        validator._check_source_keys()

        assert not _rows(validator, "Source keys")

    def test_a_broken_sources_file_adds_no_key_rows_and_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        (root / ".dango" / "sources.yml").write_text("sources: [unclosed\n", encoding="utf-8")
        validator = ProjectValidator(root)

        validator._check_source_keys()
        validator._check_source_names()

        assert not validator.results
