"""tests/unit/test_source_host_binding.py

Host-bearing source params: per-type validation on create and refusal to change them via update.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.setup_lifecycle import update_source
from dango.ingestion.sources.setup_schema import SourceSetupError
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
}
LABEL_TYPES = ["zendesk", "freshdesk", "jira", "workable"]
HOSTNAME_TYPES = ["inbox", "strapi"]
URL_TYPES = ["rest_api", "matomo"]

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
    "http://evil.example",
    "https://u:p@h.example.com",
    "https://h.example.com/#f",
    "https://h.example.com/?q",
    "https://evil.example#.x",
    "https://evil.example?",
    "evil.example",
    "ftp://h.example.com",
    "https://",
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_type", "value"),
    _cases(LABEL_TYPES, BAD_LABELS)
    + _cases(HOSTNAME_TYPES, BAD_HOSTNAMES)
    + _cases(URL_TYPES, BAD_URLS),
)
def test_create_rejects_bad_host_value(project: Path, source_type: str, value: str) -> None:
    param = HOST_TYPES[source_type][0]
    with pytest.raises(SourceSetupError) as exc:
        prepare_source(project, source_type, "src", _params(source_type, value))
    assert any(param in e for e in exc.value.errors), exc.value.errors


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
    ],
)
def test_create_accepts_loopback_http_and_other_valid_forms(
    project: Path, source_type: str, value: str
) -> None:
    param = HOST_TYPES[source_type][0]
    prepared = prepare_source(project, source_type, "src", _params(source_type, value))
    assert prepared.params[param] == value


@pytest.mark.unit
@pytest.mark.parametrize("source_type", sorted(HOST_TYPES))
def test_update_refuses_changing_host_param(project: Path, source_type: str) -> None:
    param, host, _ = HOST_TYPES[source_type]
    _create(project, source_type)
    new = "other" if source_type in LABEL_TYPES else host.replace("example", "example2")
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
