"""tests/unit/test_text_io_scanner_dotenv.py

The text-I/O scanner also flags python-dotenv reads (`load_dotenv`, `dotenv_values`, `find_dotenv`,
`DotEnv`) outside the one safe loader, so a new bare call cannot bring back the raw codec crash.
"""

import pytest

from tests.factories.text_io_scan import DOTENV_HELPER_MODULES, scan_source

pytestmark = pytest.mark.unit

HELPER = "dango/utils/project_env.py"

BARE = {
    "load_dotenv": "from dotenv import load_dotenv\nload_dotenv(p, override=False)",
    "dotenv_values": "from dotenv import dotenv_values\ndotenv_values(p)",
    "find_dotenv": "from dotenv import find_dotenv\nfind_dotenv()",
    "DotEnv": "from dotenv.main import DotEnv\nDotEnv(p)",
    "module_attribute": "import dotenv\ndotenv.load_dotenv(p)",
    "module_alias": "import dotenv as d\nd.dotenv_values(p)",
    "import_alias": "from dotenv import load_dotenv as ld\nld(p)",
    "stream_form": "from dotenv import dotenv_values\ndotenv_values(stream=s)",
    "lazy_in_function": "def f(p):\n    from dotenv import load_dotenv\n    load_dotenv(p)",
    "dotted_main": "import dotenv.main\ndotenv.main.dotenv_values(p)",
}

OK = {
    "set_key_is_not_a_read": "from dotenv import set_key\nset_key(p, 'A', 'b')",
    "other_object_method": "obj.load_dotenv(p)",
    "helper_function": "from dango.utils.project_env import load_env\nload_env(root)",
    "unrelated": "x = dict()",
}


@pytest.mark.parametrize("name", sorted(BARE))
def test_scanner_flags_a_bare_dotenv_call(name: str) -> None:
    found = scan_source(BARE[name], "dango/ingestion/x.py", check_lf=False)
    assert len(found) == 1, (name, found)
    assert ".env" in found[0] and "dango/utils/project_env.py" in found[0]
    assert found[0].startswith("dango/ingestion/x.py:")


@pytest.mark.parametrize("name", sorted(BARE))
def test_scanner_passes_the_same_call_inside_the_helper(name: str) -> None:
    assert scan_source(BARE[name], HELPER, check_lf=False) == [], name


@pytest.mark.parametrize("name", sorted(OK))
def test_scanner_accepts_other_code(name: str) -> None:
    assert scan_source(OK[name], "dango/ingestion/x.py", check_lf=False) == [], name


def test_allowlist_is_exactly_the_helper_module() -> None:
    assert DOTENV_HELPER_MODULES == (HELPER,)


def test_reports_the_line_of_each_call() -> None:
    src = "from dotenv import load_dotenv\n\n\nload_dotenv(a)\nload_dotenv(b)\n"
    found = scan_source(src, "dango/x.py", check_lf=False)
    assert [f.split(" ")[0] for f in found] == ["dango/x.py:4", "dango/x.py:5"]


def test_helper_module_exists_and_is_scanned() -> None:
    from tests.factories.text_io_scan import REPO_ROOT, scanned_files

    assert (REPO_ROOT / HELPER).is_file()
    assert (REPO_ROOT / HELPER) in scanned_files()
