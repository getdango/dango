"""dango/platform/common/metabase_admin_repair.py

Automatic, data-preserving repair of a lost Metabase admin credential (1.0.12 / R4).

When every stored copy of the Metabase admin password is rejected, Metabase's own offline
``reset-password`` CLI is the supported way back in: it needs no old password and keeps all
data (the same trusted-operator model as the container itself). The flow is:

1. stage a fresh credential in the protected store's pending slot (crash-safe),
2. stop Metabase (the CLI needs the H2 app DB), run the one-off CLI, restart Metabase
   (always, in ``finally``),
3. redeem the one-time token over HTTP, verify a login with the new password,
4. promote the pending credential and reuse the migration's SSO refresh + YAML cleanup.

Nothing here deletes a volume. Any failure leaves the previous state untouched, retains the
Metabase container running, and is reported with a secret-free reason.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.platform.common import metabase_credential_migration as _migration
from dango.security.metabase_config import load_metabase_metadata
from dango.security.metabase_credentials import MetabaseCredentialStore

_STATE_FILENAME = "metabase_admin_repair.json"
_RETRY_AFTER_SECONDS = 3600
_TOKEN_PATTERN = re.compile(r"OK \[\[\[(.+?)\]\]\]")
_CLI_TIMEOUT_SECONDS = 300


_OUTCOME_TEXT = {
    "attempted_recently": "A repair was attempted less than an hour ago.",
    "missing_admin_email": "The project has no Metabase admin email on record.",
    "missing_metabase_url": "The project has no Metabase URL on record.",
    "no_local_compose": "This project has no local docker-compose.yml.",
    "metabase_unreachable": "Metabase is not responding, so it was left untouched.",
    "metabase_not_running": "Metabase is not running; start the project first.",
    "reset_cli_failed": "Metabase's offline reset did not complete; Metabase was restarted.",
    "metabase_not_ready": "Metabase did not become ready again in time.",
    "token_not_accepted": "Metabase did not accept the reset token.",
    "new_credential_not_accepted": "The new credential could not sign in.",
    "pending_promotion_failed": "The new credential could not be saved.",
    "candidate_staging_failed": "The new credential could not be staged safely.",
}
_GENERIC_OUTCOME = "Admin access could not be restored automatically."


def describe_repair_outcome(result: dict[str, object]) -> str:
    """Plain-text, secret-free description of a repair result (unknown reason -> generic)."""
    if result.get("status") == "repaired":
        return "Metabase admin access restored."
    reason = result.get("reason")
    if isinstance(reason, str) and reason in _OUTCOME_TEXT:
        return _OUTCOME_TEXT[reason]
    return _GENERIC_OUTCOME


def repair_admin_credential(
    project_root: Path, *, ready_timeout: int = 180, force: bool = False
) -> dict[str, object]:
    """Regain Metabase admin access without touching Metabase data.

    Returns ``{"status": "repaired"}`` or ``{"status": "failed", "reason": <slug>}`` /
    ``{"status": "skipped", "reason": <slug>}``. Never raises; never includes a secret.
    ``force`` ignores the one-hour failure memory (manual command only).
    """
    root = Path(project_root)
    try:
        return _repair(root, ready_timeout, force)
    except Exception:
        return _failed(root, "unexpected_error")


def _repair(root: Path, ready_timeout: int, force: bool) -> dict[str, object]:
    metadata = load_metabase_metadata(root)
    admin = metadata.get("admin") if isinstance(metadata, dict) else None
    admin_email = admin.get("email") if isinstance(admin, dict) else None
    metabase_url = (
        metadata.get("metabase_url", "http://localhost:3000")
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(admin_email, str) or not admin_email:
        return {"status": "skipped", "reason": "missing_admin_email"}
    if not isinstance(metabase_url, str) or not metabase_url:
        return {"status": "skipped", "reason": "missing_metabase_url"}
    metabase_url = metabase_url.rstrip("/")
    if not (root / "docker-compose.yml").exists():
        return {"status": "skipped", "reason": "no_local_compose"}
    if not force and _attempted_recently(root):
        return {"status": "skipped", "reason": "attempted_recently"}

    from dango.auth.metabase_sync import generate_metabase_password
    from dango.platform.docker import get_compose_project_name
    from dango.visualization.metabase import wait_for_metabase_ready

    with _migration._migration_lock(root):
        store = MetabaseCredentialStore(root)
        # A retained pending credential (interrupted setup/migration) is tried first.
        try:
            retained = store.load_pending()
        except Exception:
            retained = None
        if retained:
            session = _migration._create_session(metabase_url, admin_email, retained)
            if session is not None:
                return _finish(root, store, metadata or {}, metabase_url, session, admin_email)

        if not _metabase_healthy(metabase_url):
            return {"status": "skipped", "reason": "metabase_unreachable"}

        env = {**os.environ, "COMPOSE_PROJECT_NAME": get_compose_project_name(root)}
        if not _metabase_running(env):
            return {"status": "skipped", "reason": "metabase_not_running"}

        new_password = generate_metabase_password()
        try:
            store.save_pending(new_password)
        except Exception:
            return _failed(root, "candidate_staging_failed")

        token: str | None = None
        try:
            _compose(root, env, "stop", "metabase", timeout=90)
            token = _run_reset_cli(root, env, admin_email)
        finally:
            # Always bring Metabase back, whatever happened above.
            _compose(root, env, "up", "-d", "metabase", timeout=120)
        if token is None:
            _discard_pending(store)
            return _failed(root, "reset_cli_failed")
        if not wait_for_metabase_ready(metabase_url, timeout=ready_timeout):
            # The token was never redeemed, so the staged password was never applied: drop it
            # (a leftover rejected pending would make the next start report
            # credential_recovery_pending). A later start re-runs the flow with a fresh token.
            _discard_pending(store)
            return _failed(root, "metabase_not_ready")

        if not _redeem_token(metabase_url, token, new_password):
            _discard_pending(store)
            return _failed(root, "token_not_accepted")
        session = _migration._create_session(metabase_url, admin_email, new_password)
        if session is None:
            return _failed(root, "new_credential_not_accepted")
        return _finish(root, store, metadata or {}, metabase_url, session, admin_email)


def _finish(
    root: Path,
    store: MetabaseCredentialStore,
    metadata: dict[str, Any],
    metabase_url: str,
    session: str,
    admin_email: str,
) -> dict[str, object]:
    """Promote the verified pending credential, refresh SSO, clean the YAML password."""
    try:
        active = store.promote_pending()
    except Exception:
        return _failed(root, "pending_promotion_failed")
    result = _migration._refresh_sso_and_cleanup(
        root, metadata, metabase_url, session, admin_email, active
    )
    if result.get("status") != "secure_rotated":
        # The new credential is promoted and works; only the follow-up tidy-up failed.
        return _failed(root, f"followup_{result.get('reason', 'failed')}")
    _clear_state(root)
    return {"status": "repaired"}


def _metabase_healthy(metabase_url: str) -> bool:
    import requests

    try:
        return requests.get(f"{metabase_url}/api/health", timeout=5).status_code == 200
    except Exception:
        return False


def _metabase_running(env: dict[str, str]) -> bool:
    """True when this project's Metabase container is running (plain ``docker ps`` + labels,
    which behaves the same under Compose v1 and v2)."""
    project = env["COMPOSE_PROJECT_NAME"]
    try:
        proc = subprocess.run(
            ["docker", "ps", "-q"]
            + ["--filter", f"label=com.docker.compose.project={project}"]
            + ["--filter", "label=com.docker.compose.service=metabase"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def _compose_prefix() -> list[str]:
    """``docker compose`` (v2) or standalone ``docker-compose`` (v1), as DockerManager does."""
    try:
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True, timeout=10
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ["docker-compose"]


def _compose(root: Path, env: dict[str, str], *args: str, timeout: int) -> None:
    subprocess.run(
        [*_compose_prefix(), *args],
        cwd=root,
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _run_reset_cli(root: Path, env: dict[str, str], admin_email: str) -> str | None:
    """Run Metabase's offline ``reset-password`` and return its one-time token."""
    try:
        proc = subprocess.run(
            [*_compose_prefix(), "run", "--rm", "--no-deps", "-T", "metabase"]
            + ["reset-password", admin_email],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    match = _TOKEN_PATTERN.search(proc.stdout or "")
    token = match.group(1) if match else None
    del proc  # CLI stdout/stderr is never kept beyond the token
    return token


def _redeem_token(metabase_url: str, token: str, password: str) -> bool:
    import requests

    try:
        response = requests.post(
            f"{metabase_url}/api/session/reset_password",
            json={"token": token, "password": password},
            timeout=15,
        )
    except Exception:
        return False
    return response.status_code == 200


def _discard_pending(store: MetabaseCredentialStore) -> None:
    try:
        store.discard_pending()
    except Exception:  # noqa: BLE001 - keep the original failure reason
        pass


def _state_path(root: Path) -> Path:
    return root / ".dango" / "state" / _STATE_FILENAME


def _attempted_recently(root: Path) -> bool:
    try:
        data: Any = json.loads(_state_path(root).read_text())
        then = datetime.fromisoformat(data["at"])
    except Exception:
        return False
    if then.tzinfo is None:
        return False
    return 0 <= (datetime.now(timezone.utc) - then).total_seconds() < _RETRY_AFTER_SECONDS


def _failed(root: Path, reason: str) -> dict[str, object]:
    state: dict[str, object] = {
        "status": "failed",
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path = _state_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass
    return {"status": "failed", "reason": reason}


def _clear_state(root: Path) -> None:
    try:
        _state_path(root).unlink()
    except OSError:
        pass
