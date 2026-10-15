"""tests/unit/test_source_credential_binding.py

Agent-created sources must not adopt an existing credential variable; credentials show their destination host.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_sources
from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.registry import get_source_metadata
from dango.ingestion.sources.setup_lifecycle import credential_requirements
from dango.ingestion.sources.setup_schema import SourceSetupError, compute_env_var_name
from dango.ingestion.sources.setup_service import apply_source, create_source, prepare_source

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
# Types an agent may create through MCP (rest_api and inbox are CLI-only; matomo, jira and
# strapi are hidden from the wizard).
MCP_SECRET_TYPES = ["freshdesk", "workable"]
AGENT_REFUSED_TYPES = ["dlt_native", "filesystem", "inbox", "jira", "matomo", "rest_api", "strapi"]


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
@pytest.mark.parametrize("source_type", MCP_SECRET_TYPES)
def test_mcp_create_refuses_existing_dotenv_credential(project: Path, source_type: str) -> None:
    names = _secret_env_names(source_type, "foo")
    assert names
    env = project / ".env"
    env.write_text(f"{names[0]}=value-belonging-to-something-else\n")
    before = env.read_bytes()

    r = mcp_sources.create_source(source_type, "foo", SECRET_TYPES[source_type][0])

    assert "error" in r, r
    assert names[0] in r["error"]
    assert "dango source add" in r["error"]
    assert env.read_bytes() == before
    assert load_config(project).sources.get_source("foo") is None


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(SECRET_TYPES))
def test_agent_prepare_refuses_existing_credential_for_every_type(
    project: Path, source_type: str
) -> None:
    names = _secret_env_names(source_type, "foo")
    (project / ".env").write_text(f"{names[-1]}=value-belonging-to-something-else\n")
    with pytest.raises(SourceSetupError) as exc:
        prepare_source(
            project,
            source_type,
            "foo",
            SECRET_TYPES[source_type][0],
            reject_existing_credentials=True,
        )
    assert any(names[-1] in e for e in exc.value.errors), exc.value.errors


@pytest.mark.unit
@pytest.mark.parametrize("source_type", AGENT_REFUSED_TYPES)
def test_mcp_create_refuses_cli_only_and_hidden_types(project: Path, source_type: str) -> None:
    r = mcp_sources.create_source(source_type, "foo", {})
    assert "error" in r, r
    assert "dango source" in r["error"]
    assert load_config(project).sources.get_source("foo") is None
    with pytest.raises(SourceSetupError) as exc:
        create_source(project, source_type, "foo", {})
    assert exc.value.errors == r["errors"]


@pytest.mark.unit
def test_mcp_create_refuses_rest_api_even_with_absolute_endpoint(project: Path) -> None:
    params = {
        "base_url": "https://api.example.com",
        "endpoints": [{"path": "https://elsewhere.example/x", "name": "x"}],
    }
    r = mcp_sources.create_source("rest_api", "foo", params)
    assert "dango source add" in r.get("error", ""), r


@pytest.mark.unit
def test_mcp_create_refuses_existing_process_env_credential(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = _secret_env_names("freshdesk", "foo")[0]
    monkeypatch.setenv(name, "value-belonging-to-something-else")

    r = mcp_sources.create_source("freshdesk", "foo", SECRET_TYPES["freshdesk"][0])

    assert "error" in r, r
    assert name in r["error"]
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
@pytest.mark.parametrize("source_type", MCP_SECRET_TYPES)
def test_mcp_create_credentials_required_include_sends_to(project: Path, source_type: str) -> None:
    params, host = SECRET_TYPES[source_type]
    r = mcp_sources.create_source(source_type, "foo", params)
    assert r.get("status") == "created", r
    env_reqs = [c for c in r["credentials_required"] if c["kind"] == "env_var"]
    assert env_reqs
    assert all(c.get("sends_to") == host for c in env_reqs), env_reqs


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(SECRET_TYPES))
def test_prepare_source_credentials_required_include_sends_to(
    project: Path, source_type: str
) -> None:
    params, host = SECRET_TYPES[source_type]
    prepared = prepare_source(project, source_type, "foo", params)
    reqs = [r for r in prepared.credentials_required if r.kind == "env_var"]
    assert reqs
    assert all(r.sends_to == host for r in reqs), reqs


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


@pytest.mark.unit
def test_mcp_update_refuses_inbox_source(project: Path) -> None:
    params = SECRET_TYPES["inbox"][0]
    apply_source(project, prepare_source(project, "inbox", "mail", params))
    r = mcp_sources.update_source("mail", {"folder": "Archive"})
    assert "dango source edit" in r.get("error", ""), r


@pytest.mark.unit
def test_cli_path_still_creates_inbox_source(project: Path) -> None:
    prepared = prepare_source(project, "inbox", "mail", SECRET_TYPES["inbox"][0])
    apply_source(project, prepared)
    assert load_config(project).sources.get_source("mail") is not None


# Agent-managed params whose name looks host-like but that do not choose a credential's host.
_NOT_HOST_PARAMS = {("google_sheets", "spreadsheet_url_or_id")}  # OAuth; Google API host fixed
_HOST_LIKE = re.compile(r"url|host|domain|server|endpoint_|bucket", re.IGNORECASE)


@pytest.mark.unit
def test_agent_creatable_host_params_are_vendor_pinned() -> None:
    """Every agent-creatable type with a host-like param sends credentials to a vendor domain.

    A type whose host is free-form must be refused by require_agent_setup_supported, otherwise
    an agent could point it (and any credential dlt resolves for it) at a host it chose.
    """
    from dango.ingestion.sources.registry import SOURCE_REGISTRY
    from dango.ingestion.sources.setup_hosts import HOST_BEARING_PARAMS, destination_host
    from dango.ingestion.sources.setup_schema import _managed_by, require_agent_setup_supported

    checked = []
    for source_type, meta in SOURCE_REGISTRY.items():
        try:
            require_agent_setup_supported(source_type)
        except SourceSetupError:
            continue
        for param in meta.get("required_params", []) + meta.get("optional_params", []):
            name = param["name"]
            if _managed_by(param, meta, source_type) != "agent" or not _HOST_LIKE.search(name):
                continue
            if (source_type, name) in _NOT_HOST_PARAMS:
                continue
            assert HOST_BEARING_PARAMS.get(source_type) == name, (source_type, name)
            # Pinned: whatever single label is given, the host stays under the vendor domain.
            host = destination_host(source_type, {name: "acme"}) or ""
            assert host.startswith("acme.") and host.count(".") >= 2, (source_type, host)
            checked.append(source_type)
    assert checked, "expected at least one agent-creatable host-bearing type"
