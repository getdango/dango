"""tests/unit/test_webhook_url_masking.py

GET /api/notifications/config masks webhook URLs for non-admins; mask_webhook_url never raises.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.auth.models import Role
from dango.platform.notifications.webhook import mask_webhook_url
from dango.web.routes.schedules import router as schedules_router
from tests.factories.auth_app_factories import AuthProject, build_client

_SECRET_URL = "https://hooks.slack.com/services/T000/B000/CANARYWEBHOOKSECRET"
_CONFIG = f"""\
notifications:
  webhooks:
    - name: ops
      url: {_SECRET_URL}
      format: slack
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    (tmp_path / ".dango" / "schedules.yml").write_text(_CONFIG)
    return p


def _get_config(project: AuthProject, role: Role) -> dict:
    user = project.add_user(f"{role.value}@example.com", role)
    client = build_client(project.root, schedules_router)
    with patch("dango.web.routes.schedules.get_project_root", return_value=project.root):
        resp = client.get(
            "/api/notifications/config", headers={"Cookie": project.session_cookie(user)}
        )
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.unit
class TestNotificationConfigMasking:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
    def test_notification_config_masks_url_for_viewer_and_editor(
        self, project: AuthProject, role: Role
    ) -> None:
        body = _get_config(project, role)

        (webhook,) = body["webhooks"]
        assert "CANARYWEBHOOKSECRET"[:-4] not in str(body)
        assert webhook["url"] == "https://hooks.slack.com/\u2026CRET"
        assert webhook["name"] == "ops"
        assert webhook["format"] == "slack"

    def test_notification_config_shows_url_to_admin(self, project: AuthProject) -> None:
        body = _get_config(project, Role.ADMIN)

        assert body["webhooks"][0]["url"] == _SECRET_URL


@pytest.mark.unit
class TestMaskWebhookUrl:
    def test_masks_path_keeps_host_and_last_four(self) -> None:
        assert mask_webhook_url(_SECRET_URL) == "https://hooks.slack.com/\u2026CRET"

    def test_userinfo_and_port_are_not_echoed(self) -> None:
        masked = mask_webhook_url("https://user:pw@example.com:8443/hook/abcd1234")

        assert masked == "https://example.com/\u20261234"
        assert "pw" not in masked

    @pytest.mark.parametrize("garbage", ["", "no-scheme", "http://", "://x", "http://[::1", "   "])
    def test_mask_webhook_url_handles_garbage(self, garbage: str) -> None:
        assert mask_webhook_url(garbage) == "****"

    def test_very_short_url_does_not_raise(self) -> None:
        assert mask_webhook_url("http://a") == "http://a/\u2026://a"
