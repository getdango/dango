"""tests/unit/test_bind_host.py

The shared bind-address decision for `dango web` and `dango serve`, and the strict reader of the
local auth toggle it depends on: only an auth.yml that says enabled: false may change the bind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_cloud_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


def _write_auth(root: Path, text: str | bytes) -> None:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    path = root / ".dango" / "auth.yml"
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _disabled(root: Path) -> bool:
    from dango.auth.admin import is_auth_explicitly_disabled

    return is_auth_explicitly_disabled(root)


def _resolve(root: Path, explicit: str | None, default_host: str = "0.0.0.0") -> Any:
    from dango.cli.helpers.bind_host import resolve_bind_host

    return resolve_bind_host(root, explicit, default_host=default_host)


class TestExplicitlyDisabledToggle:
    @pytest.mark.parametrize("text", ["enabled: false\n", "enabled: no\n", "enabled: 0\n"])
    def test_a_false_toggle_is_disabled(self, tmp_path: Path, text: str) -> None:
        _write_auth(tmp_path, text)
        assert _disabled(tmp_path) is True

    @pytest.mark.parametrize("text", ["enabled: true\n", "enabled: yes\n", "enabled: 1\n"])
    def test_a_true_toggle_is_not_disabled(self, tmp_path: Path, text: str) -> None:
        _write_auth(tmp_path, text)
        assert _disabled(tmp_path) is False

    def test_a_missing_file_is_not_disabled(self, tmp_path: Path) -> None:
        assert _disabled(tmp_path) is False

    @pytest.mark.parametrize(
        "text",
        ["", "{}\n", "other: 1\n", "enabled:\n", "enabled: null\n", "- a\n- b\n", "just text\n"],
    )
    def test_a_file_without_a_usable_toggle_is_not_disabled(
        self, tmp_path: Path, text: str
    ) -> None:
        _write_auth(tmp_path, text)
        assert _disabled(tmp_path) is False

    @pytest.mark.parametrize(
        "raw", [b"enabled: [unclosed\n", b"enabled: caf\xe9\n", b"\xff\xfe\x00"]
    )
    def test_a_malformed_or_undecodable_file_is_not_disabled(
        self, tmp_path: Path, raw: bytes
    ) -> None:
        _write_auth(tmp_path, raw)
        assert _disabled(tmp_path) is False

    def test_an_unreadable_path_is_not_disabled(self, tmp_path: Path) -> None:
        (tmp_path / ".dango" / "auth.yml").mkdir(parents=True)
        assert _disabled(tmp_path) is False


class TestResolveBindHost:
    def test_auth_disabled_without_host_binds_loopback_with_one_notice(
        self, tmp_path: Path
    ) -> None:
        _write_auth(tmp_path, "enabled: false\n")
        decision = _resolve(tmp_path, None)
        assert decision.host == "127.0.0.1"
        assert decision.message is not None
        assert decision.is_warning is False
        assert "\n" not in decision.message
        assert decision.message.isascii()
        assert "--host 0.0.0.0" in decision.message
        assert "network" in decision.message

    def test_auth_enabled_keeps_the_default_and_prints_nothing(self, tmp_path: Path) -> None:
        _write_auth(tmp_path, "enabled: true\n")
        decision = _resolve(tmp_path, None)
        assert decision.host == "0.0.0.0"
        assert decision.message is None

    def test_a_missing_auth_file_keeps_the_default(self, tmp_path: Path) -> None:
        decision = _resolve(tmp_path, None)
        assert decision.host == "0.0.0.0"
        assert decision.message is None

    @pytest.mark.parametrize("raw", [b"enabled: [unclosed\n", b"\xff\xfe\x00", b"- a\n", b""])
    def test_a_broken_auth_file_keeps_the_default(self, tmp_path: Path, raw: bytes) -> None:
        _write_auth(tmp_path, raw)
        decision = _resolve(tmp_path, None)
        assert decision.host == "0.0.0.0"
        assert decision.message is None

    def test_cloud_mode_keeps_the_default_and_prints_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        _write_auth(tmp_path, "enabled: false\n")
        decision = _resolve(tmp_path, None, default_host="127.0.0.1")
        assert decision.host == "127.0.0.1"
        assert decision.message is None
        web_style = _resolve(tmp_path, None, default_host="0.0.0.0")
        assert web_style.host == "0.0.0.0"
        assert web_style.message is None

    def test_the_default_host_is_used_when_the_rule_does_not_apply(self, tmp_path: Path) -> None:
        _write_auth(tmp_path, "enabled: true\n")
        assert _resolve(tmp_path, None, default_host="10.1.2.3").host == "10.1.2.3"

    @pytest.mark.parametrize("auth", ["enabled: false\n", "enabled: true\n", None])
    @pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "localhost", "192.168.1.20"])
    def test_an_explicit_host_always_wins(
        self, tmp_path: Path, auth: str | None, host: str
    ) -> None:
        if auth is not None:
            _write_auth(tmp_path, auth)
        assert _resolve(tmp_path, host).host == host

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "myhost.local"])
    def test_a_network_host_with_auth_disabled_gets_one_warning(
        self, tmp_path: Path, host: str
    ) -> None:
        _write_auth(tmp_path, "enabled: false\n")
        decision = _resolve(tmp_path, host)
        assert decision.message is not None
        assert decision.is_warning is True
        assert "\n" not in decision.message
        assert decision.message.isascii()
        assert host in decision.message

    @pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
    def test_a_loopback_host_with_auth_disabled_is_silent(self, tmp_path: Path, host: str) -> None:
        _write_auth(tmp_path, "enabled: false\n")
        assert _resolve(tmp_path, host).message is None

    @pytest.mark.parametrize("auth", ["enabled: true\n", None])
    def test_a_network_host_with_auth_on_or_unknown_is_silent(
        self, tmp_path: Path, auth: str | None
    ) -> None:
        if auth is not None:
            _write_auth(tmp_path, auth)
        assert _resolve(tmp_path, "0.0.0.0").message is None

    def test_a_network_host_in_cloud_mode_is_silent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        _write_auth(tmp_path, "enabled: false\n")
        assert _resolve(tmp_path, "0.0.0.0").message is None
