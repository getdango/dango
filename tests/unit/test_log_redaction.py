"""tests/unit/test_log_redaction.py

Log endpoints and MCP get_logs never return secret values held in .env or .dlt/secrets.toml.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dango.auth.models import Role
from dango.web.app import app as dango_app
from dango.web.routes import logs
from tests.factories.auth_app_factories import AuthProject, build_client

ENV_CANARY = "canary-token-9f3a1c7e52"
TOML_CANARY = "canary-secret-b81d44a9e0"
BEARER_CANARY = "canary-bearer-77aa11cc33"
URL_PASS_CANARY = "canary-urlpass-5521dd"
QUERY_CANARY = "canary-query-0e9f8d7c6b"
HOOK_CANARY = "CANARYWEBHOOKSECRET123"
ALL_CANARIES = [
    ENV_CANARY,
    TOML_CANARY,
    BEARER_CANARY,
    URL_PASS_CANARY,
    QUERY_CANARY,
    HOOK_CANARY,
]

SOURCE_LINES = [
    f"2026-10-01 10:00:00 - ERROR - request rejected, value was {ENV_CANARY} in body",
    f"2026-10-01 10:00:01 - ERROR - dsn https://u:{URL_PASS_CANARY}@h.example/x?token={QUERY_CANARY}",
    f"2026-10-01 10:00:02 - ERROR - headers dump Authorization: Bearer {BEARER_CANARY}",
    f"2026-10-01 10:00:03 - ERROR - backend said {TOML_CANARY}",
    f"2026-10-01 10:00:04 - ERROR - post https://hooks.slack.com/services/T000/B000/{HOOK_CANARY} failed",
    "unparseable line mentioning " + ENV_CANARY,
]


def _plant(root: Path) -> None:
    (root / ".env").write_text(f"FOO_TOKEN={ENV_CANARY}\nSHORT=abc\n")
    (root / ".dlt").mkdir(exist_ok=True)
    (root / ".dlt" / "secrets.toml").write_text(
        f'[sources.foo]\nclient_secret = "{TOML_CANARY}"\nflag = true\n'
    )
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "events_sync.log").write_text("\n".join(SOURCE_LINES) + "\n")
    activity = root / ".dango" / "logs"
    activity.mkdir(parents=True, exist_ok=True)
    entries = [
        {"timestamp": "t", "level": "error", "source": "events", "message": line}
        for line in SOURCE_LINES
    ]
    (activity / "activity.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthProject]:
    proj = AuthProject(tmp_path)
    proj.create_db()
    monkeypatch.setattr(dango_app.state, "project_root", tmp_path, raising=False)
    _plant(tmp_path)
    yield proj


def _get(project: AuthProject, role: Role, path: str) -> TestClient:
    project.write_toggle("enabled: true\n")
    user = project.add_user(f"{role.value}@example.com", role)
    client = build_client(project.root, logs.router)
    client.headers.update({"Cookie": project.session_cookie(user)})
    return client


@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
@pytest.mark.parametrize("path", ["/api/logs", "/api/sources/events/logs"])
def test_log_endpoints_contain_no_canaries(project: AuthProject, role: Role, path: str) -> None:
    resp = _get(project, role, path).get(path)
    assert resp.status_code == 200, resp.text
    assert resp.json(), "expected log entries"
    for canary in ALL_CANARIES:
        assert canary not in resp.text, canary


def test_redaction_marks_replaced_values(project: AuthProject) -> None:
    resp = _get(project, Role.VIEWER, "/api/logs").get("/api/logs")
    assert "[REDACTED]" in resp.text


def test_non_message_string_fields_are_redacted(project: AuthProject) -> None:
    entry = {"timestamp": "t", "level": "error", "source": ENV_CANARY, "message": "m"}
    (project.root / ".dango" / "logs" / "activity.jsonl").write_text(json.dumps(entry) + "\n")
    resp = _get(project, Role.VIEWER, "/api/logs").get("/api/logs")
    assert ENV_CANARY not in resp.text


def test_response_shape_unchanged(project: AuthProject) -> None:
    client = _get(project, Role.VIEWER, "/api/logs")
    src = client.get("/api/sources/events/logs").json()
    assert len(src) == len(SOURCE_LINES)
    assert all(set(e) == {"timestamp", "level", "message"} for e in src)
    assert src[0]["level"] == "ERROR"
    allv = client.get("/api/logs").json()
    assert len(allv) == len(SOURCE_LINES)
    assert {"timestamp", "level", "source", "message"} <= set(allv[0])


def test_short_values_not_redacted_words(project: AuthProject) -> None:
    (project.root / "logs" / "events_sync.log").write_text(
        "2026-10-01 10:00:00 - INFO - abc and abcdef words stay readable\n"
    )
    resp = _get(project, Role.VIEWER, "/api/sources/events/logs").get("/api/sources/events/logs")
    assert resp.json()[0]["message"] == "abc and abcdef words stay readable"


def test_malformed_env_and_toml_do_not_break_logs(project: AuthProject) -> None:
    (project.root / ".env").write_bytes(b"\xff\xfe=\x00garbage\n=novalue\nNOEQUALS\n")
    (project.root / ".dlt" / "secrets.toml").write_text("this is [not toml")
    client = _get(project, Role.VIEWER, "/api/logs")
    assert client.get("/api/logs").status_code == 200
    assert client.get("/api/sources/events/logs").status_code == 200


def test_secrets_reloaded_when_env_changes(project: AuthProject) -> None:
    from dango.utils.redact import load_secret_values

    first = load_secret_values(project.root)
    assert ENV_CANARY in first and TOML_CANARY in first
    assert "abc" not in first
    env = project.root / ".env"
    env.write_text("OTHER=brand-new-secret-value\n")
    import os

    stat = env.stat()
    os.utime(env, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert "brand-new-secret-value" in load_secret_values(project.root)


def test_redact_text_rules() -> None:
    from dango.utils.redact import redact_text

    out = redact_text("see https://user:pw123456@host/p and https://tokenonly@host/p", frozenset())
    assert "pw123456" not in out and "tokenonly" not in out
    assert "https://[REDACTED]@host/p" in out
    out = redact_text("GET /x?api_key=abcdef123456&other=1&signature=zzzzzz999", frozenset())
    assert "abcdef123456" not in out and "zzzzzz999" not in out and "other=1" in out
    out = redact_text("Authorization: Basic dXNlcjpwYXNz", frozenset())
    assert "dXNlcjpwYXNz" not in out
    out = redact_text("https://discord.com/api/webhooks/123456/abcTOKENxyz", frozenset())
    assert "abcTOKENxyz" not in out


def test_mcp_get_logs_still_redacts(project: AuthProject, monkeypatch: pytest.MonkeyPatch) -> None:
    from dango.cli.commands import mcp_debug

    monkeypatch.setattr(mcp_debug, "_get_project_root", lambda: project.root)
    dumped = json.dumps(mcp_debug.get_logs())
    for canary in ALL_CANARIES:
        assert canary not in dumped, canary
    dumped_src = json.dumps(mcp_debug.get_logs(log="dango"))
    assert ENV_CANARY not in dumped_src
