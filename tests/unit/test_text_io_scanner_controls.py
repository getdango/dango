"""tests/unit/test_text_io_scanner_controls.py

Positive and negative controls: the text-I/O scanner flags every call form it claims to detect.
"""

import pytest

from tests.factories.text_io_scan import scan_source

pytestmark = pytest.mark.unit

BAD_ENCODING = {
    "builtin_open": 'open("f")',
    "multiline_open": 'open(\n    "f",\n    "w",\n    newline="\\n",\n)',
    "path_open": 'from pathlib import Path\nPath("f").open("w", newline="\\n")',
    "aliased_path": 'from pathlib import Path as P\nP("f").open()',
    "keyword_mode": 'open("f", mode="r")',
    "read_text": "p.read_text()",
    "write_text": 'p.write_text("x", newline="\\n")',
    "fdopen": 'import os\nos.fdopen(fd, "w", newline="\\n")',
    "fdopen_from": 'from os import fdopen\nfdopen(fd, "w", newline="\\n")',
    "aliased_module": 'import io as i\ni.open("f")',
    "tempfile_text": 'import tempfile\ntempfile.NamedTemporaryFile("w", newline="\\n")',
    "tempfile_alias": 'from tempfile import NamedTemporaryFile as N\nN(mode="w+")',
    "subprocess_text": 'import subprocess\nsubprocess.run(["x"], text=True)',
    "subprocess_alias": 'import subprocess as sp\nsp.Popen(["x"], universal_newlines=True)',
    "subprocess_from": 'from subprocess import check_output as co\nco(["x"], text=True)',
    "subprocess_encoding_only": (
        'import subprocess\nsubprocess.run(["x"], text=True, encoding="utf-8")'
    ),
    "subprocess_dynamic_text": 'import subprocess\nsubprocess.run(["x"], text=flag)',
    "configparser": "import configparser\nc = configparser.ConfigParser()\nc.read('a.ini')",
    "file_handler": 'import logging\nlogging.FileHandler("a.log")',
}

OK_ENCODING = {
    "binary": 'open("f", "rb")',
    "binary_keyword": 'open("f", mode="wb")',
    "encoding_keyword": 'open("f", encoding="utf-8")',
    "encoding_positional": 'open("f", "r", -1, "utf-8")',
    "read_text_positional": 'p.read_text("utf-8")',
    "write_text_positional": 'p.write_text("x", "utf-8", newline="\\n")',
    "path_open_positional": 'p.open("r", -1, "utf-8")',
    "path_binary": 'p.open("rb")',
    "tempfile_default_binary": "import tempfile\ntempfile.NamedTemporaryFile()",
    "tempfile_binary": 'import tempfile\ntempfile.NamedTemporaryFile("wb")',
    "subprocess_bytes": 'import subprocess\nsubprocess.run(["x"], capture_output=True)',
    "subprocess_text_false": 'import subprocess\nsubprocess.run(["x"], text=False)',
    "subprocess_complete": (
        'import subprocess\nsubprocess.run(["x"], text=True, encoding="utf-8", errors="replace")'
    ),
    "subprocess_kwargs": 'import subprocess\nsubprocess.run(["x"], text=flag, **kw)',
    "urllib_opener": "opener.open(url)",
    "zip_member": 'zf.open("data.csv")',
    "os_open": "import os\nos.open(p, os.O_RDONLY)",
    "webbrowser": 'import webbrowser\nwebbrowser.open("http://x")',
    "tarfile": 'import tarfile\ntarfile.open("a.tgz", "w:gz")',
    "non_parser_read": "f.read()",
    "handler_encoding": 'import logging\nlogging.FileHandler("a", encoding="utf-8")',
}


@pytest.mark.parametrize("name", sorted(BAD_ENCODING))
def test_scanner_flags_each_call_form(name: str) -> None:
    assert scan_source(BAD_ENCODING[name]), name


@pytest.mark.parametrize("name", sorted(BAD_ENCODING))
def test_scanner_names_the_missing_encoding(name: str) -> None:
    found = scan_source(BAD_ENCODING[name], check_lf=False)
    assert any("encoding=" in f for f in found), (name, found)


@pytest.mark.parametrize("name", sorted(OK_ENCODING))
def test_scanner_accepts_compliant_forms(name: str) -> None:
    assert scan_source(OK_ENCODING[name], check_lf=False) == [], name


LF_BAD = {
    "write_text": 'p.write_text("x", encoding="utf-8")',
    "open_w": 'open("f", "w", encoding="utf-8")',
    "open_keyword_mode": 'open("f", mode="a", encoding="utf-8")',
    "path_open": 'p.open("w", encoding="utf-8")',
    "fdopen": 'import os\nos.fdopen(fd, "w", encoding="utf-8")',
    "newline_none": 'open("f", "w", encoding="utf-8", newline=None)',
    "newline_crlf": 'open("f", "w", encoding="utf-8", newline="\\r\\n")',
    "renamed_target": 'script = x\nopen(script, "w", encoding="utf-8")',
    "multiline": 'open(\n    f,\n    "w",\n    encoding="utf-8",\n)',
}
LF_OK = {
    "write_text": 'p.write_text("x", encoding="utf-8", newline="\\n")',
    "open_w": 'open("f", "w", encoding="utf-8", newline="\\n")',
    "read_only": 'open("f", encoding="utf-8")',
    "read_text": 'p.read_text(encoding="utf-8")',
    "binary_write": 'open("f", "wb")',
}


@pytest.mark.parametrize("name", sorted(LF_BAD))
def test_scanner_flags_text_writes_without_lf(name: str) -> None:
    found = scan_source(LF_BAD[name])
    assert any("newline" in f for f in found), (name, found)


@pytest.mark.parametrize("name", sorted(LF_OK))
def test_scanner_accepts_lf_writes(name: str) -> None:
    assert scan_source(LF_OK[name]) == [], name
