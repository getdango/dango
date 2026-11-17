"""tests/unit/test_text_io_scanner_values.py

Scanner controls for encoding/errors VALUES, argument positions, dict-carried kwargs and extra forms.
"""

import pytest

from tests.factories.text_io_scan import scan_source

pytestmark = pytest.mark.unit

_SP = "import subprocess\n"
_LOG = "import logging\nfrom logging import handlers\n"

BAD = {
    "encoding_none": 'open("f", encoding=None)',
    "encoding_locale": 'open("f", encoding="locale")',
    "encoding_cp1252": 'open("f", encoding="cp1252")',
    "encoding_positional_other": 'open("f", "r", -1, "latin-1")',
    "encoding_variable": 'open("f", encoding=enc)',
    "read_text_other": 'p.read_text(encoding="ascii")',
    "errors_variable": 'open("f", encoding="utf-8", errors=e)',
    "sub_errors_strict": _SP
    + 'subprocess.run(["x"], text=True, encoding="utf-8", errors="strict")',
    "sub_errors_ignore": _SP
    + 'subprocess.run(["x"], text=True, encoding="utf-8", errors="ignore")',
    "sub_encoding_locale": _SP
    + 'subprocess.run(["x"], text=True, encoding="locale", errors="replace")',
    "sub_errors_variable": _SP + 'subprocess.run(["x"], text=True, encoding="utf-8", errors=e)',
    "dict_text_only": _SP + 'P = {"text": True}\nsubprocess.Popen(["x"], **P)',
    "dict_annotated": _SP + 'P: dict = {"stdout": 1, "text": True}\nsubprocess.Popen(["x"], **P)',
    "dict_encoding_only": _SP
    + 'P = {"text": True, "encoding": "utf-8"}\nsubprocess.Popen(["x"], **P)',
    "dict_strict": _SP
    + 'P = {"text": True, "encoding": "utf-8", "errors": "strict"}\nsubprocess.Popen(["x"], **P)',
    "inline_dict": _SP + 'subprocess.Popen(["x"], **{"text": True})',
    "dict_and_keyword": _SP + 'P = {"text": True}\nsubprocess.run(["x"], check=True, **P)',
    "file_handler_positional_none": _LOG + 'logging.FileHandler("a.log", "a", None, True)',
    "rotating_positional_backup_count": _LOG
    + 'handlers.RotatingFileHandler("a.log", "a", 1000, 5)',
    "rotating_no_encoding": _LOG + 'handlers.RotatingFileHandler("a.log")',
    "timed_rotating_backup_count": _LOG + 'handlers.TimedRotatingFileHandler("a.log", "h", 1, 5)',
    "os_popen": 'import os\nos.popen("ls")',
    "getoutput": _SP + 'subprocess.getoutput("ls")',
    "getstatusoutput": _SP + 'subprocess.getstatusoutput("ls")',
    "getoutput_encoding_only": _SP + 'subprocess.getoutput("ls", encoding="utf-8")',
    "text_io_wrapper": "import io\nio.TextIOWrapper(buf)",
    "text_io_wrapper_from": "from io import TextIOWrapper\nTextIOWrapper(buf)",
    "pathlib_module_read": 'import pathlib\npathlib.Path("f").read_text()',
    "pathlib_module_open": 'import pathlib\npathlib.Path("f").open("w", newline="\\n")',
    "pathlib_alias_read": 'import pathlib as pl\npl.Path("f").read_text()',
    "open_alias": 'o = open\no("f")',
    "open_alias_attr": 'import io\nreader = io.open\nreader("f")',
    "partial_open": 'import functools\nfunctools.partial(open, "f")',
    "partial_open_from": 'from functools import partial\npartial(open, "f", "w")',
}

OK = {
    "dict_complete": _SP
    + 'P = {"text": True, "encoding": "utf-8", "errors": "replace"}\nsubprocess.Popen(["x"], **P)',
    "dict_complete_with_pipes": _SP
    + 'P = {"stdout": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace"}\n'
    + 'subprocess.Popen(["x"], cwd=c, **P)',
    "inline_dict_complete": _SP
    + 'subprocess.run(["x"], **{"text": True, "encoding": "utf-8", "errors": "replace"})',
    "dict_without_text": _SP + 'P = {"stdout": 1}\nsubprocess.Popen(["x"], **P)',
    "opaque_kwargs_limit": _SP + 'subprocess.Popen(["x"], **kw)',
    "opaque_call_limit": _SP + 'subprocess.Popen(["x"], text=flag, **decode(flag))',
    "file_handler_positional": _LOG + 'logging.FileHandler("a.log", "a", "utf-8")',
    "rotating_positional": _LOG + 'handlers.RotatingFileHandler("a.log", "a", 1000, 5, "utf-8")',
    "timed_positional": _LOG + 'handlers.TimedRotatingFileHandler("a.log", "h", 1, 5, "utf-8")',
    "open_newline_positional": 'open(f, "w", -1, "utf-8", None, "\\n")',
    "codecs_read": 'import codecs\ncodecs.open("f", "r", "utf-8")',
    "codecs_write_has_no_newline": 'import codecs\ncodecs.open("f", "w", "utf-8")',
    "getoutput_complete": _SP + 'subprocess.getoutput("ls", encoding="utf-8", errors="replace")',
    "text_io_wrapper_utf8": 'import io\nio.TextIOWrapper(buf, encoding="utf-8")',
    "pathlib_module_complete": 'import pathlib\npathlib.Path("f").read_text(encoding="utf-8")',
    "alias_complete": 'o = open\no("f", encoding="utf-8")',
    "partial_complete": 'import functools\nfunctools.partial(open, "f", encoding="utf-8")',
    "variable_mode_limit": "p.open(mode_var)",
    "forwarding_wrapper_limit": "def w(*a, **k):\n    return open(*a, **k)",
    "unrelated_dict": "d = {'text': True}\nprint(d)",
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_flagged_forms(name: str) -> None:
    assert scan_source(BAD[name], check_lf=False), name


@pytest.mark.parametrize("name", sorted(OK))
def test_accepted_forms(name: str) -> None:
    assert scan_source(OK[name]) == [], name
