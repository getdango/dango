"""tests/unit/test_source_host_binding.py

Host-bearing source params: per-type validation on create and refusal to change them via update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.setup_lifecycle import update_source
from dango.ingestion.sources.setup_schema import SourceSetupError, normalize_params
from dango.ingestion.sources.setup_service import apply_source, prepare_source

# source type -> (host param, a valid value, other required params)
HOST_TYPES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "rest_api": (
        "base_url",
        "https://api.example.com",
        {"endpoints": [{"path": "items", "name": "items"}]},
    ),
    "zendesk": ("subdomain", "acme", {}),
    "matomo": ("url", "https://matomo.example.com", {"site_id": 1}),
    "freshdesk": ("domain", "acme", {}),
    "jira": ("subdomain", "acme", {}),
    "workable": ("subdomain", "acme", {}),
    "inbox": ("host", "imap.example.com", {}),
    "strapi": ("domain", "cms.example.com", {"endpoints": ["articles"]}),
    "filesystem": ("bucket_url", "s3://bucket/data/", {}),
}
LABEL_TYPES = ["zendesk", "freshdesk", "jira", "workable"]
HOSTNAME_TYPES = ["inbox", "strapi"]
URL_TYPES = ["rest_api", "matomo"]
FS_TYPES = ["filesystem"]

# Leading/trailing whitespace is stripped by the existing string coercion before any check,
# so whitespace cases use embedded whitespace.
BAD_LABELS = ["evil.example#", "a.b", "x/y", "user@h", "a b", "h:1", "h#", "-h", "h?x"]
BAD_HOSTNAMES = [
    "evil.example#",
    "x/y",
    "user@h",
    "a b",
    "h:1",
    "https://h",
    "h?x",
    "a..b",
    ("a" * 64) + ".example.com",
]
BAD_URLS = [
    "https://u:p@h.example.com",
    "https://h.example.com/#f",
    "https://h.example.com/?q",
    "https://evil.example#.x",
    "https://evil.example?",
    "evil.example",
    "ftp://h.example.com",
    "https://",
    "https://h.example.com:99999",
]
BAD_BUCKETS = [
    "sftp://user@evil.example/data",
    "sftp://evil.example#/data",
    "s3://bucket/data?x",
    "ftp://h.example.com/data",
    "http://h.example.com/data",
    "sftp://bad..host/data",
]


@pytest.fixture
def project(tmp_path: Path, sample_config) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    sample_config.sources.sources = []
    save_config(sample_config, root)
    return root


def _params(source_type: str, host: str) -> dict[str, Any]:
    param, _, extra = HOST_TYPES[source_type]
    return {param: host, **extra}


def _create(project: Path, source_type: str, name: str = "src") -> None:
    param, host, _ = HOST_TYPES[source_type]
    apply_source(project, prepare_source(project, source_type, name, _params(source_type, host)))


def _cases(types: list[str], values: list[str]) -> list[tuple[str, str]]:
    return [(t, v) for t in types for v in values]


_AGENT = {"reject_existing_credentials": True, "require_https": True}


@pytest.mark.unit
@pytest.mark.parametrize("agent", [False, True], ids=["cli", "agent"])
@pytest.mark.parametrize(
    ("source_type", "value"),
    _cases(LABEL_TYPES, BAD_LABELS)
    + _cases(HOSTNAME_TYPES, BAD_HOSTNAMES)
    + _cases(URL_TYPES, BAD_URLS)
    + _cases(FS_TYPES, BAD_BUCKETS),
)
def test_create_rejects_bad_host_value(
    project: Path, source_type: str, value: str, agent: bool
) -> None:
    param = HOST_TYPES[source_type][0]
    kwargs = _AGENT if agent else {}
    with pytest.raises(SourceSetupError) as exc:
        prepare_source(project, source_type, "src", _params(source_type, value), **kwargs)
    assert any(param in e for e in exc.value.errors), exc.value.errors


@pytest.mark.unit
@pytest.mark.parametrize("source_type", URL_TYPES)
def test_plain_http_is_agent_rejected_and_cli_warned(project: Path, source_type: str) -> None:
    from dango.ingestion.sources.setup_hosts import insecure_host_warning

    param = HOST_TYPES[source_type][0]
    value = "http://api.example.com"
    with pytest.raises(SourceSetupError) as exc:
        prepare_source(project, source_type, "src", _params(source_type, value), **_AGENT)
    assert any("https://" in e for e in exc.value.errors), exc.value.errors

    prepared = prepare_source(project, source_type, "src", _params(source_type, value))
    assert prepared.params[param] == value
    assert "unencrypted" in (insecure_host_warning(source_type, value) or "")
    assert insecure_host_warning(source_type, "http://127.0.0.1:8000") is None
    assert insecure_host_warning(source_type, "https://api.example.com") is None


@pytest.mark.unit
def test_label_error_says_what_to_enter() -> None:
    with pytest.raises(SourceSetupError) as exc:
        normalize_params("freshdesk", "src", {"domain": "acme.freshdesk.com"})
    assert "e.g. acme, not acme.freshdesk.com" in exc.value.errors[0]


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(HOST_TYPES))
def test_create_accepts_valid_host_unchanged(project: Path, source_type: str) -> None:
    param, host, _ = HOST_TYPES[source_type]
    prepared = prepare_source(project, source_type, "src", _params(source_type, host))
    assert prepared.params[param] == host


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_type", "value"),
    [
        ("rest_api", "http://127.0.0.1:18999"),
        ("rest_api", "http://localhost:8080/api"),
        ("rest_api", "http://[::1]:9000"),
        ("rest_api", "https://api.example.com:8443/v1"),
        ("matomo", "http://127.0.0.1:18999"),
        ("strapi", "a.b"),
        ("inbox", "imap.gmail.com"),
        ("freshdesk", "acme-support"),
        ("filesystem", "data/exports/"),
        ("filesystem", "gs://bucket/x/"),
        ("filesystem", "sftp://files.example.com:2222/data"),
    ],
)
def test_create_accepts_loopback_http_and_other_valid_forms(
    project: Path, source_type: str, value: str
) -> None:
    param = HOST_TYPES[source_type][0]
    for kwargs in ({}, _AGENT):
        prepared = prepare_source(
            project, source_type, "src", _params(source_type, value), **kwargs
        )
        assert prepared.params[param] == value


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(HOST_TYPES))
def test_update_refuses_changing_host_param(project: Path, source_type: str) -> None:
    param, host, _ = HOST_TYPES[source_type]
    _create(project, source_type)
    new = {"filesystem": "sftp://evil.example/data"}.get(
        source_type, "other" if source_type in LABEL_TYPES else host.replace("example", "example2")
    )
    with pytest.raises(SourceSetupError) as exc:
        update_source(project, "src", {param: new})
    assert any("dango source edit" in e for e in exc.value.errors), exc.value.errors
    stored = load_config(project).sources.get_source("src")
    assert stored is not None


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(set(HOST_TYPES) - {"rest_api"}))
def test_update_refuses_hostile_host_value(project: Path, source_type: str) -> None:
    param = HOST_TYPES[source_type][0]
    _create(project, source_type)
    hostile = "https://evil.example#" if source_type in URL_TYPES else "evil.example#"
    with pytest.raises(SourceSetupError):
        update_source(project, "src", {param: hostile})


# rest_api is not editable through update_source at all (existing behaviour).
_NON_HOST_UPDATES: dict[str, dict[str, Any]] = {
    "zendesk": {"start_date": "2024-01-01"},
    "matomo": {"site_id": 2},
    "freshdesk": {"per_page": 50},
    "jira": {"page_size": 25},
    "workable": {"load_details": True},
    "inbox": {"folder": "Archive"},
    "strapi": {"endpoints": ["pages"]},
    "filesystem": {"file_glob": "*.json"},
}


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(_NON_HOST_UPDATES))
def test_update_same_host_value_is_allowed(project: Path, source_type: str) -> None:
    param, host, _ = HOST_TYPES[source_type]
    _create(project, source_type)
    result = update_source(project, "src", {param: host})
    block = result.source_config.get(source_type) or result.source_config.get("generic_config")
    assert block[param] == host


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(_NON_HOST_UPDATES))
def test_update_non_host_param_is_allowed(project: Path, source_type: str) -> None:
    param, host, _ = HOST_TYPES[source_type]
    _create(project, source_type)
    change = _NON_HOST_UPDATES[source_type]
    result = update_source(project, "src", change)
    block = result.source_config.get(source_type) or result.source_config.get("generic_config")
    assert block[param] == host
    for key, value in change.items():
        assert block[key] == value


def _rest_api_wizard_prompt(root: Path, base_url: str) -> list[str]:
    """Run the rest_api collector up to its second prompt; return the printed messages."""
    from unittest.mock import patch

    from dango.cli.source_wizard import SourceWizard

    answers = iter([{"base_url": base_url}, None])
    with (
        patch("dango.cli.source_wizard.inquirer.prompt", side_effect=lambda *a, **k: next(answers)),
        patch("dango.cli.source_wizard.console") as console,
    ):
        assert SourceWizard(root)._collect_rest_api_params("src") is None
    return [str(c.args[0]) for c in console.print.call_args_list if c.args]


@pytest.mark.unit
def test_cli_wizard_rejects_bad_rest_api_base_url(project: Path) -> None:
    printed = _rest_api_wizard_prompt(project, "https://evil.example#")
    assert any("base_url" in m and "fragment" in m for m in printed), printed


@pytest.mark.unit
def test_cli_wizard_warns_on_plain_http_base_url(project: Path) -> None:
    printed = _rest_api_wizard_prompt(project, "http://api.example.com")
    assert any("unencrypted" in m for m in printed), printed
    assert not any(
        "unencrypted" in m for m in _rest_api_wizard_prompt(project, "https://api.example.com")
    )
