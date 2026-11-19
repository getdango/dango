"""tests/unit/test_tls_errors.py

Certificate-failure detection and guidance (TLS-inspecting proxies), and its wiring into each download site.
"""

from __future__ import annotations

import ssl
import subprocess
import sys
import urllib.error
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from rich.console import Console

from tests.factories.driver_fakes import patch_network, pin_driver

pytestmark = pytest.mark.unit

_CERT = ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
)
_FORBIDDEN = ("verify=False", "--trusted-host", "PYTHONHTTPSVERIFY", "disable", "CERT_NONE")


def _guidance() -> str:
    from dango.utils.tls_errors import certificate_guidance_lines

    return "\n".join(certificate_guidance_lines())


# --- detection ------------------------------------------------------------------------


def test_detects_every_certificate_shape() -> None:
    from dango.utils.tls_errors import is_certificate_error

    wrapped = urllib.error.URLError(_CERT)
    req = requests.exceptions.SSLError("HTTPSConnectionPool: CERTIFICATE_VERIFY_FAILED")
    chained = RuntimeError("download failed")
    chained.__cause__ = wrapped
    assert is_certificate_error(_CERT)
    assert is_certificate_error(wrapped)
    assert is_certificate_error(req)
    assert is_certificate_error(chained)
    assert is_certificate_error(OSError("unable to get local issuer certificate"))
    assert is_certificate_error(OSError("self-signed certificate in certificate chain"))


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionResetError("connection reset by peer"),
        urllib.error.URLError(OSError(11001, "getaddrinfo failed")),
        TimeoutError("timed out"),
        ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number"),
        requests.exceptions.SSLError("[SSL: WRONG_VERSION_NUMBER]"),
        ValueError("certificate"),
    ],
)
def test_other_errors_are_not_certificate_errors(exc: BaseException) -> None:
    from dango.utils.tls_errors import is_certificate_error

    assert is_certificate_error(exc) is False


def test_text_detection_covers_pip_output() -> None:
    from dango.utils.tls_errors import is_certificate_error_text

    assert is_certificate_error_text(
        "WARNING: Retrying ... SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]"
    )
    assert not is_certificate_error_text("ERROR: No matching distribution found for getdango==9")
    assert not is_certificate_error_text("")


def test_cyclic_exception_chain_terminates() -> None:
    from dango.utils.tls_errors import is_certificate_error

    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert is_certificate_error(a) is False


# --- guidance text --------------------------------------------------------------------


def test_guidance_names_the_supported_fixes() -> None:
    text = _guidance()
    for needle in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "pip", "--cert", "IT", "firewall"):
        assert needle in text, needle


def test_guidance_never_suggests_turning_verification_off() -> None:
    text = _guidance()
    for bad in _FORBIDDEN:
        assert bad not in text, bad
    text.encode("cp1252")


def test_print_guidance_is_literal_under_rich() -> None:
    from dango.utils.tls_errors import print_certificate_guidance

    buf = StringIO()
    print_certificate_guidance(Console(file=buf, width=200, force_terminal=False))
    assert "REQUESTS_CA_BUNDLE" in buf.getvalue()


# --- driver download ------------------------------------------------------------------


@pytest.fixture
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_driver(monkeypatch)


def test_driver_download_raises_a_certificate_error(_pinned: None, tmp_path: Path) -> None:
    from dango.utils import driver

    with patch_network([urllib.error.URLError(_CERT)]):
        with pytest.raises(driver.DriverDownloadError) as info:
            driver.download_verified_driver(tmp_path / "metabase-plugins")
    assert type(info.value).__name__ == "DriverCertificateError"
    assert "certificate" in str(info.value).lower()
    assert not any(bad in str(info.value) for bad in _FORBIDDEN)


def test_driver_plain_network_failure_is_not_a_certificate_error(
    _pinned: None, tmp_path: Path
) -> None:
    from dango.utils import driver

    with patch_network([urllib.error.URLError(OSError("getaddrinfo failed"))]):
        with pytest.raises(driver.DriverDownloadError) as info:
            driver.download_verified_driver(tmp_path / "metabase-plugins")
    assert type(info.value).__name__ == "DriverDownloadError"


def test_init_prints_guidance_for_a_certificate_failure(
    _pinned: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.cli.init import ProjectInitializer

    with patch_network([urllib.error.URLError(_CERT)]):
        ok = ProjectInitializer(tmp_path)._setup_metabase()
    out = capsys.readouterr().out
    assert ok is False
    assert "REQUESTS_CA_BUNDLE" in out and "SSL_CERT_FILE" in out
    assert "Traceback" not in out
    assert not any(bad in out for bad in _FORBIDDEN)


def test_init_network_failure_without_certificate_has_no_guidance(
    _pinned: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.cli.init import ProjectInitializer

    with patch_network([urllib.error.URLError(OSError("getaddrinfo failed"))]):
        ProjectInitializer(tmp_path)._setup_metabase()
    assert "REQUESTS_CA_BUNDLE" not in capsys.readouterr().out


def test_start_prints_guidance_and_exits_1(
    _pinned: None,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from dango.cli.main import cli
    from tests.factories.scaffold_project import build_fresh_project, working_copy

    root = working_copy(build_fresh_project(tmp_path_factory), tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    assert not (root / "metabase-plugins" / "duckdb.metabase-driver.jar").exists()
    with patch_network([urllib.error.URLError(_CERT)]):
        result = CliRunner().invoke(cli, ["start", "--yes"])
    assert result.exit_code == 1, result.output
    assert "REQUESTS_CA_BUNDLE" in result.output and "SSL_CERT_FILE" in result.output
    assert "Traceback" not in result.output
    assert not any(bad in result.output for bad in _FORBIDDEN)


# --- spaCy model download -------------------------------------------------------------


def test_spacy_download_certificate_failure_prints_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.governance import pii_detector

    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = OSError("model not found")
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setattr(pii_detector, "_analyzer", None)
    monkeypatch.setattr(pii_detector, "_analyzer_init_failed", False)
    with patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError(_CERT)):
        assert pii_detector._get_analyzer() is None
    out = capsys.readouterr().out
    assert "REQUESTS_CA_BUNDLE" in out and "SSL_CERT_FILE" in out
    assert "Traceback" not in out


def test_spacy_download_other_failure_has_no_certificate_guidance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.governance import pii_detector

    fake_spacy = MagicMock()
    fake_spacy.load.side_effect = OSError("model not found")
    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setattr(pii_detector, "_analyzer", None)
    monkeypatch.setattr(pii_detector, "_analyzer_init_failed", False)
    with patch("urllib.request.urlretrieve", side_effect=urllib.error.URLError(OSError("down"))):
        assert pii_detector._get_analyzer() is None
    assert "REQUESTS_CA_BUNDLE" not in capsys.readouterr().out


# --- source dependency install (pip) ---------------------------------------------------


def test_dependency_install_certificate_failure_prints_guidance(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dango.utils.tls_errors import print_guidance_for_install_failure

    stderr = b"SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]'))"
    error = subprocess.CalledProcessError(1, ["pip"], stderr=stderr)
    assert print_guidance_for_install_failure(error) is True
    out = capsys.readouterr().out
    assert "REQUESTS_CA_BUNDLE" in out and "--cert" in out


def test_dependency_install_other_failure_prints_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dango.utils.tls_errors import print_guidance_for_install_failure

    error = subprocess.CalledProcessError(1, ["pip"], stderr=b"No matching distribution")
    assert print_guidance_for_install_failure(error) is False
    assert capsys.readouterr().out == ""
