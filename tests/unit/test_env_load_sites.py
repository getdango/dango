"""tests/unit/test_env_load_sites.py

Every library path that reads the project `.env` turns an unreadable file into the one friendly
`EnvFileError` text (names the file, says UTF-8) instead of a raw codec error or a traceback.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dango.config.helpers import save_config

pytestmark = pytest.mark.unit

CP1252_ENV = b"caf\xe9=1\n"
RAW = ("codec", "0xe9", "invalid continuation", "Traceback")


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real project (one CSV source 'test_source') whose `.env` is cp1252."""
    save_config(sample_config, tmp_path)
    (tmp_path / ".env").write_bytes(CP1252_ENV)
    for var in ("STRIPE_TEST_API_KEY", "DANGO_OAUTH_CALLBACK_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _assert_friendly(text: str) -> None:
    flat = " ".join(text.split())
    assert "Cannot read" in flat and ".env" in flat and "UTF-8" in flat, text
    for raw in RAW:
        assert raw not in text, text


def test_sync_runner_fails_each_source_with_the_friendly_text(project: Path) -> None:
    from dango.config.helpers import get_config
    from dango.ingestion import run_sync

    sources = get_config(project).sources.sources
    summary = run_sync(project_root=project, sources=sources, skip_dbt=True)
    assert summary["failed_count"] == len(sources) >= 1
    assert summary["success_count"] == 0
    for failed in summary["failed_sources"]:
        _assert_friendly(failed["error"])


def test_runner_construction_does_not_raise_for_a_bad_env(project: Path) -> None:
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner(project)
    assert runner.project_root == project


def test_credential_checks_raise_the_env_file_error(project: Path) -> None:
    from dango.utils.project_env import EnvFileError

    from dango.ingestion.credential_health import run_credential_checks

    with pytest.raises(EnvFileError) as info:
        run_credential_checks(project)
    _assert_friendly(str(info.value))


def test_prepare_source_reports_the_env_file_as_a_setup_error(project: Path) -> None:
    from dango.ingestion.sources.setup_service import SourceSetupError, prepare_source

    with pytest.raises(SourceSetupError) as info:
        prepare_source(project, "stripe", "stripe_test", {})
    assert any("Cannot read" in e for e in info.value.errors), info.value.errors
    _assert_friendly("\n".join(info.value.errors))


def test_credential_requirements_raise_the_env_file_error(project: Path) -> None:
    from dango.utils.project_env import EnvFileError

    from dango.config.models import DataSource, SourceType
    from dango.ingestion.sources.setup_lifecycle import credential_requirements

    source = DataSource(name="stripe_test", type=SourceType.STRIPE)
    with pytest.raises(EnvFileError):
        credential_requirements(project, source)


def test_validate_env_vars_check_fails_with_the_friendly_text(project: Path) -> None:
    from dango.cli.validate import ProjectValidator

    validator = ProjectValidator(project)
    validator._check_env_vars()
    rows = [r for r in validator.results if "Env" in r.name]
    assert rows and rows[0].status == "fail", validator.results
    _assert_friendly(rows[0].message)


def test_script_env_check_reports_the_env_file_and_does_not_crash(project: Path) -> None:
    from dango.cli.validate_script_env import check_script_env

    (project / "scripts").mkdir()
    (project / "scripts" / "a.py").write_text("pass\n", encoding="utf-8")
    (project / ".dango" / "scripts.yml").write_text(
        "scripts:\n  - path: a.py\n    env: [SLACK_TOKEN]\n", encoding="utf-8"
    )
    results = check_script_env(project)
    _assert_friendly(" ".join(r[2] for r in results))
    assert all(r[1] != "fail" for r in results)


def test_oauth_manager_raises_the_env_file_error(project: Path) -> None:
    from dango.utils.project_env import EnvFileError

    from dango.oauth import OAuthManager

    with pytest.raises(EnvFileError) as info:
        OAuthManager(project)
    _assert_friendly(str(info.value))


def test_google_provider_prints_the_friendly_text_not_a_traceback(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    (project / ".env").write_bytes(b"WP37_FINE=1\n")
    provider = GoogleOAuthProvider(OAuthManager(project))
    (project / ".env").write_bytes(CP1252_ENV)
    assert provider.authenticate("google_sheets") is None
    captured = capsys.readouterr()
    _assert_friendly(captured.out + captured.err)


def test_wizard_database_check_raises_the_env_file_error(project: Path) -> None:
    from dango.utils.project_env import EnvFileError

    from dango.cli.source_wizard import SourceWizard

    wizard = SourceWizard(project)
    wizard.secret_params = [{"param_name": "credentials_env", "name": "PG_URL"}]
    with pytest.raises(EnvFileError):
        wizard._check_postgres_connections()


def test_bad_env_leaves_the_process_environment_alone(project: Path) -> None:
    from dango.utils.project_env import EnvFileError

    from dango.oauth import OAuthManager

    before = dict(os.environ)
    with pytest.raises(EnvFileError):
        OAuthManager(project)
    assert dict(os.environ) == before
