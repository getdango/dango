"""tests/unit/test_source_host_binding_paths.py

Host binding per entry path: agent flags, service create_source, secrets.toml sends_to, rest_api wizard input.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.setup_schema import SourceSetupError, normalize_params
from dango.ingestion.sources.setup_service import create_source

_URL_EXTRA: dict[str, dict[str, Any]] = {
    "rest_api": {"endpoints": [{"path": "items", "name": "items"}]},
    "matomo": {"site_id": 1},
}
_URL_PARAM = {"rest_api": "base_url", "matomo": "url"}


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    return root


def _url_params(source_type: str, url: str) -> dict[str, Any]:
    return {_URL_PARAM[source_type]: url, **_URL_EXTRA[source_type]}


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(_URL_PARAM))
def test_normalize_params_require_https(source_type: str) -> None:
    param = _URL_PARAM[source_type]
    with pytest.raises(SourceSetupError) as exc:
        normalize_params(
            source_type,
            "src",
            _url_params(source_type, "http://api.example.com"),
            require_https=True,
        )
    assert any(param in e and "https://" in e for e in exc.value.errors), exc.value.errors
    for ok in ("https://api.example.com", "http://127.0.0.1:8000", "http://localhost:9000"):
        out, _ = normalize_params(
            source_type, "src", _url_params(source_type, ok), require_https=True
        )
        assert out[param] == ok
    out, _ = normalize_params(
        source_type, "src", _url_params(source_type, "http://api.example.com")
    )
    assert out[param] == "http://api.example.com"


@pytest.mark.unit
def test_mcp_create_passes_agent_flags_to_prepare_source(project: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_prepare(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise SourceSetupError(["stop"])

    with patch("dango.ingestion.sources.setup_service.prepare_source", fake_prepare):
        r = mcp_sources.create_source("freshdesk", "foo", {"domain": "acme"})
    assert r.get("error") == "stop", r
    assert seen.get("reject_existing_credentials") is True
    assert seen.get("require_https") is True


@pytest.mark.unit
def test_service_create_source_refuses_existing_credential(project: Path) -> None:
    (project / ".env").write_text("FOO_API_KEY=value-belonging-to-something-else\n")
    with pytest.raises(SourceSetupError) as exc:
        create_source(project, "freshdesk", "foo", {"domain": "acme"})
    assert any("FOO_API_KEY" in e for e in exc.value.errors), exc.value.errors
    assert load_config(project).sources.get_source("foo") is None


@pytest.mark.unit
def test_service_create_source_passes_require_https(project: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_prepare(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise SourceSetupError(["stop"])

    with patch("dango.ingestion.sources.setup_service.prepare_source", fake_prepare):
        with pytest.raises(SourceSetupError):
            create_source(project, "freshdesk", "foo", {"domain": "acme"})
    assert seen.get("require_https") is True
    assert seen.get("reject_existing_credentials") is True


@pytest.mark.unit
def test_zendesk_secrets_toml_requirement_shows_tenant_host(project: Path) -> None:
    r = mcp_sources.create_source("zendesk", "foo", {"subdomain": "acme"})
    assert r.get("status") == "created", r
    toml_reqs = [c for c in r["credentials_required"] if c["kind"] == "secrets_toml"]
    assert toml_reqs, r["credentials_required"]
    assert all(c["sends_to"] == "acme.zendesk.com" for c in toml_reqs), toml_reqs


def _wizard_base_url(root: Path, *entries: str) -> tuple[list[str], int]:
    """Feed base_url entries to the rest_api collector, then cancel the next prompt."""
    from dango.cli.source_wizard import SourceWizard

    answers = iter([{"base_url": e} for e in entries] + [None])
    calls = 0

    def prompt(*_a: Any, **_k: Any) -> Any:
        nonlocal calls
        calls += 1
        return next(answers)

    with (
        patch("dango.cli.source_wizard.inquirer.prompt", side_effect=prompt),
        patch("dango.cli.source_wizard.console") as console,
    ):
        assert SourceWizard(root)._collect_rest_api_params("src") is None
    return [str(c.args[0]) for c in console.print.call_args_list if c.args], calls


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url",
    [
        "http://my_api:8000",
        "https://api.example.com.",
        "https://internal_service.example.com/v2",
    ],
)
def test_cli_wizard_accepts_human_entered_base_urls(project: Path, base_url: str) -> None:
    printed, calls = _wizard_base_url(project, base_url)
    assert not any("❌" in m for m in printed), printed
    assert calls == 2  # moved on to the auth-type prompt


@pytest.mark.unit
@pytest.mark.parametrize(
    "base_url", ["http://my_api:8000", "https://api.example.com/v1?x=1", "https://api.example.com."]
)
def test_agent_path_rules_unchanged_for_lenient_inputs(base_url: str) -> None:
    with pytest.raises(SourceSetupError):
        normalize_params("rest_api", "src", _url_params("rest_api", base_url), require_https=True)


@pytest.mark.unit
def test_cli_wizard_reprompts_after_bad_base_url(project: Path) -> None:
    printed, calls = _wizard_base_url(project, "https://evil.example#", "https://api.example.com")
    assert any("❌" in m and "base_url" in m for m in printed), printed
    assert calls == 3  # bad entry, re-entry, then the auth-type prompt


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["https://api.example.com/v1?api-version=2", "https://api.example.com/v1#frag"]
)
def test_cli_wizard_refuses_query_or_fragment_and_reprompts(project: Path, bad: str) -> None:
    """A query in base_url breaks every request at sync (the endpoint path is appended after it)."""
    printed, calls = _wizard_base_url(project, bad, "https://api.example.com/v1")
    errors = [m for m in printed if "❌" in m]
    assert errors, printed
    assert "Put query parameters in the endpoint's params, not the base URL" in errors[0]
    assert calls == 3  # bad entry, re-entry, then the auth-type prompt
