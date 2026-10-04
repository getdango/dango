"""tests/unit/test_metabase_admin_repair.py

Verify the automatic, data-preserving Metabase admin repair: sequence, restart invariant,
pending-first, skips, failure memory, redaction and the no-deletion guarantee.
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest
import yaml

import dango.platform.common.metabase_admin_repair as repair_module
import dango.security.metabase_credentials as credentials
from dango.auth.database import create_user, get_user_by_email
from dango.auth.metabase_sync import decrypt_metabase_password, encrypt_metabase_password
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner
from dango.platform.common.metabase_admin_repair import (
    describe_repair_outcome,
    repair_admin_credential,
)
from dango.security.metabase_credentials import MetabaseCredentialStore

EMAIL = "admin@example.com"
URL = "http://localhost:3000"
STALE = "stale-yml-secret"
NEW_PASSWORD = "sentinel-new-password"
TOKEN = "sentinel-reset-token"
CLI_NOISE = "sentinel-cli-noise"


def _subcommand(argv: list[str]) -> str:
    """Operation name of a recorded docker call (``docker ps`` / ``docker[ -]compose <sub>``)."""
    if argv[:2] == ["docker", "ps"]:
        return "ps"
    return argv[2] if argv[:2] == ["docker", "compose"] else argv[1]


class Harness:
    """Records every docker/HTTP call and scripts their results."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.argv: list[list[str]] = []
        self.running = True
        self.compose_v1 = False
        self.healthy = True
        self.cli_mode = "ok"  # ok | fail | timeout | raise
        self.ready = True
        self.redeem_status = 200
        self.accepted: set[str] = {NEW_PASSWORD}

    def run(self, argv: list[str], **_kwargs: Any) -> Any:
        proc = MagicMock(returncode=0, stdout="", stderr="")
        if argv[:3] == ["docker", "compose", "version"]:
            if self.compose_v1:
                proc.returncode = 1
            return proc  # the v2-vs-v1 probe is not one of the recorded operations
        self.argv.append(list(argv))
        sub = _subcommand(argv)
        self.events.append(sub)
        if sub == "ps":
            proc.stdout = "container-id\n" if self.running else ""
        elif sub == "run":
            if self.cli_mode == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            if self.cli_mode == "raise":
                raise OSError("docker missing")
            if self.cli_mode == "fail":
                proc.returncode = 1
            else:
                proc.stdout = f"{CLI_NOISE}\nOK [[[{TOKEN}]]]\n"
        return proc

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.events.append("health")
        return MagicMock(status_code=200 if self.healthy else 503)

    def post(self, url: str, json: dict[str, str], timeout: int) -> Any:
        response = MagicMock()
        if url.endswith("/api/session/reset_password"):
            self.events.append("redeem")
            assert json["token"] == TOKEN
            response.status_code = self.redeem_status
            return response
        ok = json["password"] in self.accepted
        self.events.append("login")
        response.status_code = 200 if ok else 401
        response.json.return_value = {"id": f"session-{json['password']}"} if ok else {}
        return response

    def wait_ready(self, *_args: Any, **_kwargs: Any) -> bool:
        self.events.append("ready")
        return self.ready

    @property
    def subcommands(self) -> list[str]:
        return [_subcommand(a) for a in self.argv]


@pytest.fixture
def project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": URL, "admin": {"email": EMAIL, "password": STALE}}),
        encoding="utf-8",
    )
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump(
            {
                "project": {
                    "name": "T",
                    "id": "repair-test-id",
                    "created_by": "test@example.com",
                    "purpose": "Unit test",
                }
            }
        ),
        encoding="utf-8",
    )
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(
        db_path=dango_dir / "auth.db", db_name="auth", migrations_dir=migrations_dir
    ).apply_pending()
    create_user(
        dango_dir / "auth.db",
        User(
            email=EMAIL,
            password_hash="$2b$12$fakehashfakehashfakehashfakehashfakehashfakehashfakeh",
            role=Role.ADMIN,
            metabase_user_id=7,
            metabase_password_enc=encrypt_metabase_password("old-sso-secret", root),
        ),
    )
    broken_keyring = Mock()
    for name in ("get_password", "set_password", "delete_password"):
        getattr(broken_keyring, name).side_effect = RuntimeError("no keyring")
    monkeypatch.setattr(credentials, "keyring", broken_keyring)
    monkeypatch.setattr("dango.security.token_storage.keyring", broken_keyring)
    monkeypatch.setattr(credentials, "_LOCAL_SECRETS_DIR", tmp_path / "secrets")
    return root


@pytest.fixture
def harness() -> Iterator[Harness]:
    h = Harness()
    with ExitStack() as stack:
        stack.enter_context(patch.object(repair_module.subprocess, "run", side_effect=h.run))
        stack.enter_context(patch("requests.get", side_effect=h.get))
        stack.enter_context(patch("requests.post", side_effect=h.post))
        stack.enter_context(
            patch("dango.visualization.metabase.wait_for_metabase_ready", side_effect=h.wait_ready)
        )
        stack.enter_context(
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-test")
        )
        stack.enter_context(
            patch("dango.auth.metabase_sync.generate_metabase_password", return_value=NEW_PASSWORD)
        )
        stack.enter_context(
            patch("dango.auth.metabase_sync.find_metabase_user_by_email", return_value={"id": 7})
        )
        yield h


def _state(root: Path) -> Path:
    return root / ".dango" / "state" / "metabase_admin_repair.json"


def _yml(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((root / ".dango" / "metabase.yml").read_text())
    return data


@pytest.mark.unit
def test_repair_sequence_stops_runs_cli_restarts_then_redeems(
    project_root: Path, harness: Harness
) -> None:
    result = repair_admin_credential(project_root)

    assert result == {"status": "repaired"}
    assert harness.subcommands == ["ps", "stop", "run", "up"]
    assert harness.argv[1][-2:] == ["stop", "metabase"]
    assert harness.argv[2][3:7] == ["--rm", "--no-deps", "-T", "metabase"]
    assert harness.argv[2][-2:] == ["reset-password", EMAIL]
    assert harness.argv[3][-3:] == ["up", "-d", "metabase"]
    order = [e for e in harness.events if e in {"up", "ready", "redeem"}]
    assert order == ["up", "ready", "redeem"]


@pytest.mark.unit
def test_repair_success_promotes_credential_refreshes_sso_and_cleans_yaml(
    project_root: Path, harness: Harness
) -> None:
    _state(project_root).parent.mkdir(parents=True, exist_ok=True)
    _state(project_root).write_text("{}")

    assert repair_admin_credential(project_root, force=True) == {"status": "repaired"}

    store = MetabaseCredentialStore(project_root)
    assert store.load() == NEW_PASSWORD
    assert store.load_pending() is None
    assert "password" not in _yml(project_root)["admin"]
    user = get_user_by_email(project_root / ".dango" / "auth.db", EMAIL)
    assert user is not None and user.metabase_password_enc
    assert decrypt_metabase_password(user.metabase_password_enc, project_root) == NEW_PASSWORD
    migration_state = json.loads(
        (project_root / ".dango" / "state" / "metabase_credential_migration.json").read_text()
    )
    assert migration_state["status"] == "secure_rotated"
    assert not _state(project_root).exists()


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["fail", "timeout", "raise"])
def test_metabase_is_restarted_when_the_cli_fails(
    project_root: Path, harness: Harness, mode: str
) -> None:
    harness.cli_mode = mode
    store = MetabaseCredentialStore(project_root)
    store.save("existing-active")

    result = repair_admin_credential(project_root)

    assert result == {"status": "failed", "reason": "reset_cli_failed"}
    assert harness.subcommands[-1] == "up"
    assert "redeem" not in harness.events
    assert store.load_pending() is None
    assert store.load() == "existing-active"


@pytest.mark.unit
def test_metabase_is_restarted_even_when_stop_raises(project_root: Path, harness: Harness) -> None:
    real = harness.run

    def run(argv: list[str], **kwargs: Any) -> Any:
        if argv[2] == "stop":
            harness.argv.append(list(argv))
            raise subprocess.TimeoutExpired(argv, 1)
        return real(argv, **kwargs)

    with patch.object(repair_module.subprocess, "run", side_effect=run):
        result = repair_admin_credential(project_root)

    assert result["status"] == "failed"
    assert harness.subcommands[-1] == "up"


@pytest.mark.unit
def test_token_not_accepted_discards_pending(project_root: Path, harness: Harness) -> None:
    harness.redeem_status = 400

    result = repair_admin_credential(project_root)

    assert result == {"status": "failed", "reason": "token_not_accepted"}
    assert MetabaseCredentialStore(project_root).load_pending() is None
    assert MetabaseCredentialStore(project_root).load() is None


@pytest.mark.unit
def test_metabase_not_ready_discards_the_unapplied_pending_and_reports(
    project_root: Path, harness: Harness
) -> None:
    harness.ready = False

    result = repair_admin_credential(project_root)

    assert result == {"status": "failed", "reason": "metabase_not_ready"}
    # The token was never redeemed: a leftover pending would make the next start report
    # credential_recovery_pending, so it must not be kept.
    assert MetabaseCredentialStore(project_root).load_pending() is None
    assert "redeem" not in harness.events


@pytest.mark.unit
def test_retained_pending_credential_is_tried_first_and_avoids_the_cli(
    project_root: Path, harness: Harness
) -> None:
    MetabaseCredentialStore(project_root).save_pending("retained-pending")
    harness.accepted = {"retained-pending"}

    result = repair_admin_credential(project_root)

    assert result == {"status": "repaired"}
    assert harness.argv == []  # no ps/stop/run/up at all
    store = MetabaseCredentialStore(project_root)
    assert store.load() == "retained-pending"
    user = get_user_by_email(project_root / ".dango" / "auth.db", EMAIL)
    assert user is not None and user.metabase_password_enc
    assert decrypt_metabase_password(user.metabase_password_enc, project_root) == "retained-pending"


@pytest.mark.unit
def test_rejected_pending_falls_through_to_cli(project_root: Path, harness: Harness) -> None:
    MetabaseCredentialStore(project_root).save_pending("retained-but-rejected")

    result = repair_admin_credential(project_root)

    assert result == {"status": "repaired"}
    assert harness.subcommands == ["ps", "stop", "run", "up"]
    assert MetabaseCredentialStore(project_root).load() == NEW_PASSWORD


@pytest.mark.unit
class TestSkips:
    def test_no_compose_file(self, project_root: Path, harness: Harness) -> None:
        (project_root / "docker-compose.yml").unlink()
        assert repair_admin_credential(project_root) == {
            "status": "skipped",
            "reason": "no_local_compose",
        }
        assert harness.argv == []

    def test_missing_admin_email(self, project_root: Path, harness: Harness) -> None:
        (project_root / ".dango" / "metabase.yml").write_text(
            yaml.safe_dump({"metabase_url": URL, "admin": {}})
        )
        assert repair_admin_credential(project_root)["reason"] == "missing_admin_email"
        assert harness.argv == []

    def test_missing_url(self, project_root: Path, harness: Harness) -> None:
        (project_root / ".dango" / "metabase.yml").write_text(
            yaml.safe_dump({"metabase_url": "", "admin": {"email": EMAIL}})
        )
        assert repair_admin_credential(project_root)["reason"] == "missing_metabase_url"
        assert harness.argv == []

    def test_metabase_unreachable_is_never_stopped(
        self, project_root: Path, harness: Harness
    ) -> None:
        harness.healthy = False
        assert repair_admin_credential(project_root) == {
            "status": "skipped",
            "reason": "metabase_unreachable",
        }
        assert "stop" not in harness.subcommands and "up" not in harness.subcommands

    def test_metabase_not_running_is_left_alone(self, project_root: Path, harness: Harness) -> None:
        harness.running = False
        assert repair_admin_credential(project_root) == {
            "status": "skipped",
            "reason": "metabase_not_running",
        }
        assert harness.subcommands == ["ps"]

    def test_attempted_recently_and_force_override(
        self, project_root: Path, harness: Harness
    ) -> None:
        harness.cli_mode = "fail"
        assert repair_admin_credential(project_root)["reason"] == "reset_cli_failed"
        harness.argv.clear()

        assert repair_admin_credential(project_root) == {
            "status": "skipped",
            "reason": "attempted_recently",
        }
        assert harness.argv == []

        assert repair_admin_credential(project_root, force=True)["reason"] == "reset_cli_failed"
        assert "stop" in harness.subcommands


@pytest.mark.unit
def test_failure_memory_written_without_secrets(project_root: Path, harness: Harness) -> None:
    harness.cli_mode = "fail"
    repair_admin_credential(project_root)

    data = json.loads(_state(project_root).read_text())
    assert set(data) == {"status", "reason", "at"}
    assert data["status"] == "failed" and data["reason"] == "reset_cli_failed"


@pytest.mark.unit
def test_no_secret_in_results_state_or_logs(
    project_root: Path,
    harness: Harness,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    outputs: list[str] = []
    for mode in ("fail", "ok"):
        harness.cli_mode = mode
        result = repair_admin_credential(project_root, force=True)
        outputs.append(json.dumps(result))
        outputs.append(describe_repair_outcome(result))
        if _state(project_root).exists():
            outputs.append(_state(project_root).read_text())
    harness.ready = False
    harness.cli_mode = "ok"
    outputs.append(json.dumps(repair_admin_credential(project_root, force=True)))
    if _state(project_root).exists():
        outputs.append(_state(project_root).read_text())
    captured = capsys.readouterr()
    outputs += [captured.out, captured.err, caplog.text]

    blob = "\n".join(outputs)
    for secret in (NEW_PASSWORD, TOKEN, CLI_NOISE, STALE):
        assert secret not in blob


@pytest.mark.unit
def test_module_never_deletes_anything() -> None:
    tree = ast.parse(Path(repair_module.__file__).read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }
    for forbidden in ("volume", "down", "prune", "rm", "-f", "-v", "kill", "system"):
        assert forbidden not in literals
    # The only docker operations the module may name: ps (plain docker), the compose
    # subcommands stop/run/up, and the compose version probe.
    assert {"ps", "stop", "run", "up", "version"} <= literals
    assert literals & {"exec", "cp", "kill", "pause", "restart", "create", "build"} == set()


@pytest.mark.unit
def test_describe_repair_outcome_is_secret_free_and_has_a_default() -> None:
    assert "restored" in describe_repair_outcome({"status": "repaired"})
    known = describe_repair_outcome({"status": "failed", "reason": "reset_cli_failed"})
    generic = describe_repair_outcome({"status": "failed", "reason": "something_new"})
    assert known != generic
    assert describe_repair_outcome({"status": "failed"}) == generic
    assert describe_repair_outcome({"status": "failed", "reason": f"x{TOKEN}"}) == generic


@pytest.mark.unit
def test_standalone_docker_compose_v1_is_used_when_the_plugin_is_missing(
    project_root: Path, harness: Harness
) -> None:
    """DockerManager falls back to `docker-compose`; so must the repair (not skip or fail)."""
    harness.compose_v1 = True

    result = repair_admin_credential(project_root)

    assert result == {"status": "repaired"}
    compose_heads = [a[:1] for a in harness.argv if a[:2] != ["docker", "ps"]]
    assert compose_heads and all(head == ["docker-compose"] for head in compose_heads)
    assert harness.subcommands == ["ps", "stop", "run", "up"]
