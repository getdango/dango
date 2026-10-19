"""tests/unit/test_audit_coverage.py

Every state-changing route writes an audit event or has an explained ALLOWLIST entry; fixed gaps write the expected event.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.routing import iter_route_contexts

from dango.auth.models import Role, User
from dango.auth.sessions import create_session
from dango.web.app import app
from dango.web.routes import catalog, dbt, initial_sync, notebooks
from dango.web.routes.auth import router as auth_router
from dango.web.routes.auth_2fa import router as auth_2fa_router
from tests.factories.auth_app_factories import AuthProject, build_client

_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_DEPTH = 3

# (method or "*", path) -> why this state-changing route has no audit call in its handler (or helpers it calls).
ALLOWLIST: dict[tuple[str, str], str] = {
    # --- no state worth attributing ---
    (
        "POST",
        "/api/notebooks/{name}/heartbeat",
    ): "lock keep-alive ping; the lock itself is the attributed action",
    (
        "POST",
        "/api/auth/2fa/setup",
    ): "own account: stores a pending secret; TWO_FA_ENABLED is logged by totp.enable_totp on verify-setup",
    # --- loopback-only internal call, no user ---
    (
        "POST",
        "/api/internal/schedules/reload",
    ): "internal loopback call made by the scheduler CLI; the user-facing reload route is audited",
    # --- code in the shared Marimo process cannot be attributed per cell/user ---
    (
        "*",
        "/notebooks/marimo/{path:path}",
    ): "reverse proxy to the shared Marimo process; only notebook create/delete/lock events are attributed",
    (
        "*",
        "/notebooks/marimo/",
    ): "reverse proxy to the shared Marimo process; same as the {path} route",
    # --- Metabase reverse proxies: Metabase enforces its own roles and keeps its own audit trail ---
    ("*", "/api/health"): "Metabase API proxy catch (also the public liveness probe)",
    ("*", "/api/session"): "Metabase API proxy",
    ("*", "/api/user"): "Metabase API proxy",
    ("*", "/api/database"): "Metabase API proxy",
    ("*", "/api/database/{path:path}"): "Metabase API proxy",
    ("*", "/metabase"): "Metabase proxy",
    ("*", "/metabase/{path:path}"): "Metabase proxy",
}


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _resolve(call: ast.Call, namespace: dict[str, Any]) -> Any:
    """Resolve ``helper(...)`` or ``module.helper(...)`` to a dango function, if it is one."""
    func = call.func
    target: Any = None
    if isinstance(func, ast.Name):
        target = namespace.get(func.id)
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = namespace.get(func.value.id)
        if isinstance(owner, types.ModuleType):
            target = getattr(owner, func.attr, None)
    module = getattr(target, "__module__", "") or ""
    if inspect.isfunction(target) and module.startswith("dango."):
        return target
    return None


def _audit_events(fn: Any, depth: int = 0, seen: set[Any] | None = None) -> list[str]:
    """Names of ``AuditEvent`` members logged by *fn* or by dango helpers it calls (AST)."""
    seen = seen if seen is not None else set()
    if fn in seen or depth > _MAX_DEPTH:
        return []
    seen.add(fn)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError):
        return []
    namespace = getattr(fn, "__globals__", {})
    events: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node) == "log_auth_event":
            first = node.args[0] if node.args else None
            events.append(first.attr if isinstance(first, ast.Attribute) else "<dynamic>")
            continue
        helper = _resolve(node, namespace)
        if helper is None:
            continue
        nested = _audit_events(helper, depth + 1, seen)
        if "<dynamic>" in nested:
            # A wrapper such as ``_audit(event, ...)``: the event is the caller's argument.
            passed = [a.attr for a in node.args if _is_audit_event(a)]
            nested = [e for e in nested if e != "<dynamic>"] + (passed or ["<dynamic>"])
        events.extend(nested)
    return events


def _is_audit_event(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "AuditEvent"
    )


def _state_changing_routes() -> list[tuple[str, str, Any]]:
    found = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.route
        methods = set(getattr(route, "methods", None) or ()) & _STATE_CHANGING
        for method in sorted(methods):
            found.append((method, route.path, route.endpoint))
    return found


def _allowlisted(method: str, path: str) -> bool:
    return (method, path) in ALLOWLIST or ("*", path) in ALLOWLIST


def _unaudited() -> list[tuple[str, str]]:
    return [(m, p) for m, p, fn in _state_changing_routes() if not _audit_events(fn)]


@pytest.mark.unit
class TestAuditCoverage:
    def test_every_state_changing_route_is_audited_or_allowlisted(self) -> None:
        missing = [(m, p) for m, p in _unaudited() if not _allowlisted(m, p)]

        assert not missing, (
            "State-changing routes with no log_auth_event call and not in ALLOWLIST: "
            f"{missing}. Log an existing AuditEvent member or add an ALLOWLIST entry with a reason."
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        unaudited = set(_unaudited())
        unaudited_paths = {p for _m, p in unaudited}
        stale = [
            key
            for key in ALLOWLIST
            if key not in unaudited and not (key[0] == "*" and key[1] in unaudited_paths)
        ]

        assert not stale, f"ALLOWLIST entries that now have an audit call or no route: {stale}"

    def test_allowlist_entries_have_reasons(self) -> None:
        assert all(reason.strip() for reason in ALLOWLIST.values())

    def test_detector_positive_controls(self) -> None:
        by_key = {(m, p): fn for m, p, fn in _state_changing_routes()}

        assert "SCRIPT_RUN" in _audit_events(by_key[("POST", "/api/scripts/{name:path}/run")])
        assert "NOTEBOOK_CREATED" in _audit_events(by_key[("POST", "/api/notebooks")])
        assert "TWO_FA_DISABLED" in _audit_events(by_key[("POST", "/api/auth/2fa/disable")])
        assert not _audit_events(by_key[("POST", "/api/notebooks/{name}/heartbeat")])

    def test_script_and_notebook_actions_are_attributed(self) -> None:
        by_key = {(m, p): fn for m, p, fn in _state_changing_routes()}

        expected = {
            ("POST", "/api/scripts/{name:path}/run"): "SCRIPT_RUN",
            ("POST", "/api/scripts/{name:path}/cancel"): "SCRIPT_CANCELLED",
            ("POST", "/api/notebooks"): "NOTEBOOK_CREATED",
            ("POST", "/api/notebooks/{name}/copy"): "NOTEBOOK_CREATED",
            ("DELETE", "/api/notebooks/{name}"): "NOTEBOOK_DELETED",
            ("DELETE", "/api/notebooks/{name}/lock"): "NOTEBOOK_LOCK_FORCE_RELEASED",
            ("POST", "/api/notebooks/marimo/restart"): "NOTEBOOK_MARIMO_RESTARTED",
            ("POST", "/api/catalog/{source}/{table}/profile"): "CATALOG_PROFILE_TRIGGERED",
            ("POST", "/api/initial-sync/start"): "SYNC_TRIGGERED",
            ("DELETE", "/api/auth/sessions/{session_id}"): "SESSION_EXPIRED",
            ("POST", "/api/dbt/models/{model_name}/run"): "DBT_MODEL_RUN",
            ("POST", "/api/notebooks/{name}/lock"): "NOTEBOOK_LOCKED",
            ("POST", "/api/notebooks/{name}/release"): "NOTEBOOK_RELEASED",
            ("POST", "/api/auth/2fa/regenerate-recovery"): "RECOVERY_CODES_REGENERATED",
        }
        for key, event in expected.items():
            assert event in _audit_events(by_key[key]), key


def _audit_entries(project_root: Path) -> list[dict[str, Any]]:
    path = project_root / ".dango" / "logs" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _request(host: str = "203.0.113.7", headers: dict[str, str] | None = None) -> Any:
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host), headers=headers or {})


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


@pytest.mark.unit
class TestFixedGapsWriteEvents:
    def test_table_profile_logs_catalog_profile_triggered(self, project: AuthProject) -> None:
        user = User(email="ed@example.com", role=Role.EDITOR, password_hash="x")
        with (
            patch.object(catalog, "get_project_root", return_value=project.root),
            patch.object(catalog, "_validate_and_resolve", new=AsyncMock(return_value=Path("x"))),
            patch.object(catalog, "profile_table", return_value={}),
            patch.object(catalog, "_get_column_schema", return_value=[]),
            patch.object(catalog, "_get_row_count", return_value=0),
            patch.object(catalog, "_get_profiled_at", return_value=None),
        ):
            asyncio.run(catalog.refresh_table_profile("src", "tbl", _request(), user))

        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["catalog_profile_triggered"]
        assert entries[0]["email"] == "ed@example.com"
        assert entries[0]["details"] == {"source": "src", "table": "tbl"}

    def test_notebook_copy_logs_notebook_created(self, project: AuthProject) -> None:
        user = User(email="ed@example.com", role=Role.EDITOR, password_hash="x")
        with (
            patch.object(notebooks, "get_project_root", return_value=project.root),
            patch.object(notebooks, "copy_locked_notebook", return_value="nb_copy_ed"),
        ):
            resp = asyncio.run(notebooks.copy_notebook("nb", _request(), user))

        assert resp.status_code == 201
        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["notebook_created"]
        assert entries[0]["email"] == "ed@example.com"
        assert entries[0]["details"] == {"notebook_name": "nb_copy_ed", "copied_from": "nb"}

    def test_own_session_revoke_logs_session_expired(self, project: AuthProject) -> None:
        user = project.add_user("ed@example.com", Role.EDITOR)
        token, _current = create_session(project.db_path, user.id)
        _other_token, other = create_session(project.db_path, user.id)
        client = build_client(project.root, auth_router)

        resp = client.delete(
            f"/api/auth/sessions/{other.id}",
            headers={"Cookie": f"dango_session={token}", "X-Requested-With": "test"},
        )

        assert resp.status_code == 200
        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["session_expired"]
        assert entries[0]["email"] == "ed@example.com"
        assert entries[0]["details"] == {"session_id": other.id, "scope": "own"}

    def test_initial_sync_start_with_deploy_token_logs_sync_triggered(
        self, project: AuthProject
    ) -> None:
        source = types.SimpleNamespace(name="s1", type=types.SimpleNamespace(value="csv"))
        config = types.SimpleNamespace(sources=types.SimpleNamespace(sources=[source]))
        request = _request(headers={"authorization": "Bearer tok"})
        with (
            patch.object(initial_sync, "_validate_deploy_token", return_value=True),
            patch.object(initial_sync, "get_project_root", return_value=project.root),
            patch("dango.config.helpers.load_config", return_value=config),
        ):
            result = asyncio.run(
                initial_sync.start_initial_sync(
                    request, types.SimpleNamespace(add_task=lambda *a, **k: None)
                )
            )

        assert result["status"] == "started"
        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["sync_triggered"]
        assert entries[0]["details"] == {"initial_sync": True, "auth": "deploy_token", "sources": 1}


@pytest.mark.unit
class TestNewEventsWired:
    def test_dbt_model_run_logs_dbt_model_run(self, project: AuthProject) -> None:
        user = User(email="ed@example.com", role=Role.EDITOR, password_hash="x")
        manifest = {"nodes": {"model.p.m1": {"resource_type": "model", "name": "m1"}}}
        tasks = types.SimpleNamespace(add_task=lambda *a, **k: None)
        with (
            patch.object(dbt, "get_project_root", return_value=project.root),
            patch.object(dbt, "get_dbt_manifest", return_value=manifest),
            patch.object(dbt.ws_manager, "broadcast", new=AsyncMock()),
        ):
            asyncio.run(
                dbt.run_dbt_model("m1", tasks, request=_request(), cascade=False, user=user)
            )

        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["dbt_model_run"]
        assert entries[0]["email"] == "ed@example.com"
        assert entries[0]["details"] == {"model_name": "m1", "cascade": False}

    def _lock(self, project: AuthProject, acquired: bool) -> Any:
        user = User(email="ed@example.com", role=Role.EDITOR, password_hash="x")
        (project.root / "notebooks").mkdir()
        (project.root / "notebooks" / "nb.py").write_text("# nb\n")
        with (
            patch.object(notebooks, "get_project_root", return_value=project.root),
            patch.object(notebooks, "acquire_lock", return_value=acquired),
            patch.object(notebooks, "get_lock_info", return_value=None),
            patch.object(notebooks, "ensure_marimo_protected"),
            patch.object(notebooks, "get_marimo_status", return_value={"running": False}),
            patch.object(notebooks, "_start_marimo_background", new=AsyncMock()),
            patch.object(notebooks, "start_idle_checker"),
        ):
            return asyncio.run(notebooks.lock_notebook("nb", _request(), user))

    def test_notebook_lock_logs_notebook_locked(self, project: AuthProject) -> None:
        self._lock(project, acquired=True)

        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["notebook_locked"]
        assert entries[0]["details"] == {"notebook_name": "nb"}

    def test_refused_notebook_lock_logs_nothing(self, project: AuthProject) -> None:
        resp = self._lock(project, acquired=False)

        assert resp.status_code == 409
        assert _audit_entries(project.root) == []

    @pytest.mark.parametrize(("released", "expected"), [(True, ["notebook_released"]), (False, [])])
    def test_notebook_release_logs_only_on_release(
        self, project: AuthProject, released: bool, expected: list[str]
    ) -> None:
        user = User(email="ed@example.com", role=Role.EDITOR, password_hash="x")
        with (
            patch.object(notebooks, "get_project_root", return_value=project.root),
            patch.object(notebooks, "release_lock", return_value=released),
        ):
            asyncio.run(notebooks.release_notebook_lock("nb", _request(), user))

        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == expected

    def test_recovery_regeneration_logs_event_without_codes(self, project: AuthProject) -> None:
        user = project.add_user("ed@example.com", Role.EDITOR)
        client = build_client(project.root, auth_2fa_router)
        stored = types.SimpleNamespace(totp_enabled=True, totp_secret="SECRETSEED")
        codes = ["AAAA-BBBB", "CCCC-DDDD"]
        with (
            patch("dango.web.routes.auth_2fa.verify_password", return_value=True),
            patch("dango.web.routes.auth_2fa.get_user_by_id", return_value=stored),
            patch("dango.web.routes.auth_2fa.verify_totp_code", return_value=True),
            patch("dango.web.routes.auth_2fa.regenerate_recovery_codes", return_value=codes),
        ):
            resp = client.post(
                "/api/auth/2fa/regenerate-recovery",
                json={"password": "pw-secret-value", "code": "123456"},
                headers={"Cookie": project.session_cookie(user), "X-Requested-With": "test"},
            )

        assert resp.status_code == 200
        entries = _audit_entries(project.root)
        assert [e["event"] for e in entries] == ["recovery_codes_regenerated"]
        assert entries[0]["email"] == "ed@example.com"
        log_text = (project.root / ".dango" / "logs" / "audit.jsonl").read_text()
        for secret in [*codes, "pw-secret-value", "123456", "SECRETSEED"]:
            assert secret not in log_text

    def test_failed_recovery_regeneration_logs_nothing(self, project: AuthProject) -> None:
        user = project.add_user("ed@example.com", Role.EDITOR)
        client = build_client(project.root, auth_2fa_router)
        with patch("dango.web.routes.auth_2fa.verify_password", return_value=False):
            resp = client.post(
                "/api/auth/2fa/regenerate-recovery",
                json={"password": "wrong", "code": "123456"},
                headers={"Cookie": project.session_cookie(user), "X-Requested-With": "test"},
            )

        assert resp.status_code == 400
        assert _audit_entries(project.root) == []
