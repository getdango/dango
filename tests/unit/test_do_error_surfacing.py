"""tests/unit/test_do_error_surfacing.py

DigitalOcean API errors surface DO's own message, and 403s point at token scopes.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from dango.exceptions import CloudAPIError, CloudProvisioningError
from dango.platform.cloud.digitalocean import DigitalOceanClient

TOKEN = "dop_v1_supersecrettoken1234567890"


def _response(status: int, body: str | None, json_data=None) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status
    mock.is_success = 200 <= status < 300
    mock.headers = {}
    mock.text = body or ""
    if json_data is not None:
        mock.json.return_value = json_data
    else:
        mock.json.side_effect = ValueError("not json")
    return mock


def _http_client(response: MagicMock) -> MagicMock:
    http = MagicMock()
    http.__enter__ = MagicMock(return_value=http)
    http.__exit__ = MagicMock(return_value=False)
    http.request.return_value = response
    return http


def _call(response: MagicMock) -> CloudAPIError:
    client = DigitalOceanClient(token=TOKEN, max_retries=0)
    http = _http_client(response)
    with patch("dango.platform.cloud.digitalocean.httpx.Client", return_value=http):
        with pytest.raises(CloudAPIError) as excinfo:
            client._request_with_retry("POST", "/droplets", json={"name": "x"})
    return excinfo.value


@pytest.mark.unit
def test_api_error_includes_do_message():
    payload = {"id": "forbidden", "message": "You are missing the required permission tag:create."}
    err = _call(_response(403, json.dumps(payload), payload))
    assert "tag:create" in str(err)
    assert "(forbidden)" in str(err)
    assert "HTTP 403" in str(err)
    assert err.status_code == 403
    assert err.is_permission_error is True


@pytest.mark.unit
def test_non_json_body_falls_back():
    err = _call(_response(422, "<html>bad</html>"))
    assert str(err) == "DigitalOcean API error: HTTP 422"
    assert err.response_body == "<html>bad</html>"
    assert err.is_permission_error is False


@pytest.mark.unit
def test_non_dict_json_body_falls_back():
    err = _call(_response(400, "[1]", [1]))
    assert str(err) == "DigitalOcean API error: HTTP 400"


@pytest.mark.unit
def test_long_message_truncated():
    payload = {"id": "x", "message": "m" * 1000}
    err = _call(_response(422, json.dumps(payload), payload))
    assert "m" * 300 in str(err)
    assert "m" * 301 not in str(err)


@pytest.mark.unit
def test_token_not_in_error_text():
    payload = {"id": "forbidden", "message": "nope"}
    err = _call(_response(403, json.dumps(payload), payload))
    assert TOKEN not in str(err)
    assert "Bearer" not in str(err)


def _run_failing_provision(tmp_path, monkeypatch, exc: Exception) -> str:
    from dango.cli.commands.deploy_provision import run_provisioning
    from dango.cli.commands.deploy_wizard import WizardConfig

    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
    config = WizardConfig(
        region="nyc1",
        size_slug="s-2vcpu-4gb",
        size_tier=None,
        domain=None,
        admin_email="admin@test.com",
        admin_password="strongpassword123",
        skip_oauth=True,
        enable_backups=False,
        monthly_cost=24,
    )
    mock_client = MagicMock()
    mock_client.upload_ssh_key.return_value = {"id": 111}
    mock_ssh = MagicMock()
    mock_ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA..."
    printed: list[str] = []
    fake_console = MagicMock()
    fake_console.print.side_effect = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    with (
        patch("dango.cli.commands.deploy_provision.console", fake_console),
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=mock_client),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=mock_ssh),
        patch("dango.platform.cloud.provisioning.provision_droplet", side_effect=exc),
        pytest.raises(CloudProvisioningError),
    ):
        run_provisioning(tmp_path, config)
    return "\n".join(printed)


@pytest.mark.unit
def test_provision_failure_403_prints_scopes_not_transient_text(tmp_path, monkeypatch):
    exc = CloudAPIError(
        "DigitalOcean API error: HTTP 403: missing tag:create (forbidden)", status_code=403
    )
    out = _run_failing_provision(tmp_path, monkeypatch, exc)
    assert "missing a permission" in out
    assert "tag (create, read)" in out
    assert "transient" not in out


@pytest.mark.unit
def test_provision_failure_other_4xx_no_retry_advice(tmp_path, monkeypatch):
    exc = CloudAPIError("DigitalOcean API error: HTTP 422: bad size (x)", status_code=422)
    out = _run_failing_provision(tmp_path, monkeypatch, exc)
    assert "bad size" in out
    assert "transient" not in out


@pytest.mark.unit
def test_5xx_still_says_transient(tmp_path, monkeypatch):
    exc = CloudAPIError("DigitalOcean API error: HTTP 503", status_code=503)
    out = _run_failing_provision(tmp_path, monkeypatch, exc)
    assert "transient" in out


def _prereqs_output(tmp_path, monkeypatch, status: int) -> str:
    from dango.cli.commands.deploy_wizard import _step_prereqs

    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "sources.yml").write_text("sources: []\n")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "test-token")
    resp = httpx.Response(status, request=httpx.Request("GET", "https://api.digitalocean.com"))
    printed: list[str] = []
    fake_console = MagicMock()
    fake_console.print.side_effect = lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    with (
        patch("httpx.get", return_value=resp),
        patch("dango.cli.commands.deploy_wizard.console", fake_console),
    ):
        _step_prereqs(tmp_path)
    return "\n".join(printed)


@pytest.mark.unit
def test_wizard_403_on_account_is_not_a_warning(tmp_path, monkeypatch):
    out = _prereqs_output(tmp_path, monkeypatch, 403)
    assert "scoped token" in out
    assert "Warning" not in out


@pytest.mark.unit
def test_wizard_other_http_error_warning_includes_status(tmp_path, monkeypatch):
    out = _prereqs_output(tmp_path, monkeypatch, 500)
    assert "Warning" in out
    assert "500" in out
