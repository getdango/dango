"""tests/unit/test_spacy_message.py

A missing or uninstallable spaCy model gives one clear message (what is skipped, the exact manual
command for this interpreter, that the rest of Dango works), never a traceback.
"""

from __future__ import annotations

import ssl
import subprocess
import sys
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.factories.duckdb_holder import make_warehouse

pytestmark = pytest.mark.unit

_COMMAND = f"{sys.executable} -m spacy download en_core_web_sm"


@pytest.fixture(autouse=True)
def _fresh_analyzer() -> Iterator[None]:
    from dango.governance import pii_detector

    pii_detector._analyzer = None
    pii_detector._analyzer_init_failed = False
    yield
    pii_detector._analyzer = None
    pii_detector._analyzer_init_failed = False


def _flat(text: str) -> str:
    return " ".join(text.split())


def _all_output(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return _flat(captured.out + "\n" + captured.err)


def _assert_message(output: str) -> None:
    assert "PII scan skipped" in output
    assert "en_core_web_sm" in output
    assert "rest of Dango" in output
    assert Path(sys.executable).name in output
    if " " not in sys.executable:
        assert _COMMAND in output
    assert "-m spacy download en_core_web_sm" in output
    assert "Traceback" not in output
    output.encode("cp1252")  # safe on a cp1252 console


def _missing_model_failed_download() -> list[Any]:
    return [
        patch("spacy.load", side_effect=OSError("[E050] Can't find model 'en_core_web_sm'")),
        patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError("offline")),
    ]


def _run_with(patches: list[Any]) -> Any:
    from dango.governance import pii_detector

    for p in patches:
        p.start()
    try:
        return pii_detector._get_analyzer()
    finally:
        patch.stopall()


class TestMessage:
    def test_missing_model_and_failed_download(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _run_with(_missing_model_failed_download()) is None
        _assert_message(_all_output(capsys))

    def test_failed_pip_install(self, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
        def fake_retrieve(url: str, dest: str) -> None:
            Path(dest).write_bytes(b"not a wheel")

        patches = [
            patch("spacy.load", side_effect=OSError("missing")),
            patch("urllib.request.urlretrieve", side_effect=fake_retrieve),
            patch("subprocess.check_call", side_effect=subprocess.CalledProcessError(1, ["pip"])),
        ]
        assert _run_with(patches) is None
        _assert_message(_all_output(capsys))

    def test_spacy_not_installed_at_all(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch.dict(sys.modules, {"spacy": None}):
            from dango.governance import pii_detector

            assert pii_detector._get_analyzer() is None
        _assert_message(_all_output(capsys))

    def test_message_is_printed_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        from dango.governance import pii_detector

        for p in _missing_model_failed_download():
            p.start()
        try:
            assert pii_detector._get_analyzer() is None
            first = _all_output(capsys)
            assert pii_detector._get_analyzer() is None
            assert pii_detector._get_analyzer() is None
        finally:
            patch.stopall()
        assert first.count("PII scan skipped") == 1
        assert _all_output(capsys) == ""

    def test_certificate_failure_adds_guidance_without_repeating_the_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cert = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
        patches = [
            patch("spacy.load", side_effect=OSError("missing")),
            patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError(cert)),
        ]
        assert _run_with(patches) is None
        output = _all_output(capsys)
        _assert_message(output)
        assert output.count("PII scan skipped") == 1
        assert "SSL_CERT_FILE" in output

    def test_analyzer_start_failure_is_also_quiet(self, capsys: pytest.CaptureFixture[str]) -> None:
        patches = [
            patch("spacy.load", return_value=object()),
            patch("presidio_analyzer.AnalyzerEngine", side_effect=RuntimeError("engine broke")),
        ]
        assert _run_with(patches) is None
        output = _all_output(capsys)
        assert "PII scan skipped" in output
        assert "rest of Dango" in output
        assert "Traceback" not in output


class TestCallersContinue:
    def test_scan_continues_over_all_tables_and_prints_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import duckdb

        from dango.governance import pii_detector

        db = make_warehouse(tmp_path / "data" / "warehouse.duckdb")
        conn = duckdb.connect(str(db))
        try:
            conn.execute("CREATE TABLE raw_test_csv.\"second\" AS SELECT 'x' AS note")
        finally:
            conn.close()
        for p in _missing_model_failed_download():
            p.start()
        try:
            findings = pii_detector.scan_sources_for_pii(tmp_path, ["test_csv"])
        finally:
            patch.stopall()
        assert findings == []
        output = _all_output(capsys)
        assert output.count("PII scan skipped") == 1
        assert "Traceback" not in output

    def test_mcp_scan_pii_names_the_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.commands import mcp_governance

        make_warehouse(tmp_path / "data" / "warehouse.duckdb")
        monkeypatch.setattr(mcp_governance, "_get_project_root", lambda: tmp_path)
        monkeypatch.setattr(mcp_governance, "_check_source", lambda root, source: None)
        for p in _missing_model_failed_download():
            p.start()
        try:
            result = mcp_governance.scan_pii("test_csv")
        finally:
            patch.stopall()
        text = _flat(result["error"])
        assert "-m spacy download en_core_web_sm" in text
        assert Path(sys.executable).name in text


def test_helper_module_is_portable() -> None:
    from dango.utils import spacy_model

    source = Path(spacy_model.__file__).read_text(encoding="utf-8")
    for banned in ("fcntl", "dango.platform.cloud", "dango.runtime", "termios", "import pwd"):
        assert banned not in source
    assert not any(ord(ch) > 127 for ch in source)
    command = spacy_model.spacy_install_command()
    assert command.endswith("-m spacy download en_core_web_sm")
    assert Path(sys.executable).name in command
