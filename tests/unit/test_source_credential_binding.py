"""tests/unit/test_source_credential_binding.py

Agent-created sources must not adopt an existing credential variable; credentials show their destination host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.registry import get_source_metadata
from dango.ingestion.sources.setup_lifecycle import credential_requirements
from dango.ingestion.sources.setup_schema import compute_env_var_name
from dango.ingestion.sources.setup_service import apply_source, prepare_source

# source type -> (agent params, expected destination host)
SECRET_TYPES: dict[str, tuple[dict[str, Any], str]] = {
    "rest_api": (
        {
            "base_url": "https://api.example.com/v1",
            "endpoints": [{"path": "items", "name": "items"}],
        },
        "api.example.com",
    ),
    "matomo": ({"url": "https://matomo.example.com", "site_id": 1}, "matomo.example.com"),
    "freshdesk": ({"domain": "acme"}, "acme.freshdesk.com"),
    "jira": ({"subdomain": "acme"}, "acme.atlassian.net"),
    "workable": ({"subdomain": "acme"}, "acme.workable.com"),
    "inbox": ({"host": "imap.example.com"}, "imap.example.com"),
    "strapi": ({"domain": "cms.example.com", "endpoints": ["articles"]}, "cms.example.com"),
}


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    monkeypatch.setattr(mcp_sources, "_get_project_root", lambda: root)
    monkeypatch.setattr(mcp_sources, "_git_warnings", lambda project_root: [])
    return root


def _secret_env_names(source_type: str, source_name: str) -> list[str]:
    meta = get_source_metadata(source_type) or {}
    return [
        compute_env_var_name(p, source_name)
        for p in meta.get("required_params", []) + meta.get("optional_params", [])
        if p.get("type") == "secret" or str(p.get("name", "")).endswith("_env")
    ]


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(SECRET_TYPES))
def test_mcp_create_refuses_existing_dotenv_credential(project: Path, source_type: str) -> None:
    names = _secret_env_names(source_type, "foo")
    assert names
    env = project / ".env"
    env.write_text(f"{names[0]}=value-belonging-to-something-else\n")
    before = env.read_bytes()

    r = mcp_sources.create_source(source_type, "foo", SECRET_TYPES[source_type][0])

    assert "error" in r, r
    assert names[0] in r["error"]
    assert env.read_bytes() == before
    assert load_config(project).sources.get_source("foo") is None


@pytest.mark.unit
def test_mcp_create_refuses_existing_process_env_credential(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _secret_env_names("rest_api", "foo")[0]
    monkeypatch.setenv(name, "value-belonging-to-something-else")

    r = mcp_sources.create_source("rest_api", "foo", SECRET_TYPES["rest_api"][0])

    assert "error" in r, r
    assert not (project / ".env").exists()
    assert load_config(project).sources.get_source("foo") is None


@pytest.mark.unit
def test_mcp_create_without_existing_credential_still_works(project: Path) -> None:
    r = mcp_sources.create_source("freshdesk", "foo", SECRET_TYPES["freshdesk"][0])
    assert r.get("status") == "created", r
    assert load_config(project).sources.get_source("foo") is not None


@pytest.mark.unit
def test_non_agent_prepare_source_keeps_adopting_existing_credential(project: Path) -> None:
    """Default (non-agent) path: an existing variable is adopted, as before."""
    name = _secret_env_names("freshdesk", "foo")[0]
    (project / ".env").write_text(f"{name}=already-set\n")
    prepared = prepare_source(project, "freshdesk", "foo", SECRET_TYPES["freshdesk"][0])
    assert prepared.params["api_secret_key_env"] == name
    assert all(r.name != name for r in prepared.credentials_required)
    apply_source(project, prepared)
    assert load_config(project).sources.get_source("foo") is not None


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(SECRET_TYPES))
def test_mcp_create_credentials_required_include_sends_to(project: Path, source_type: str) -> None:
    params, host = SECRET_TYPES[source_type]
    r = mcp_sources.create_source(source_type, "foo", params)
    assert r.get("status") == "created", r
    env_reqs = [c for c in r["credentials_required"] if c["kind"] == "env_var"]
    assert env_reqs
    assert all(c.get("sends_to") == host for c in env_reqs), env_reqs


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(SECRET_TYPES))
def test_credential_requirements_include_sends_to(project: Path, source_type: str) -> None:
    params, host = SECRET_TYPES[source_type]
    apply_source(project, prepare_source(project, source_type, "foo", params))
    source = load_config(project).sources.get_source("foo")
    reqs = [r for r in credential_requirements(project, source) if r.kind == "env_var"]
    assert reqs
    assert all(getattr(r, "sends_to", None) == host for r in reqs), reqs


@pytest.mark.unit
def test_zendesk_has_no_env_secret_to_bind(project: Path) -> None:
    """Zendesk credentials live in .dlt/secrets.toml; there is no env-var credential."""
    r = mcp_sources.create_source("zendesk", "foo", {"subdomain": "acme"})
    assert r.get("status") == "created", r
    assert not [c for c in r["credentials_required"] if c["kind"] == "env_var"]
