"""tests/unit/test_install_sh_hooks.py

install.sh test hooks: DANGO_INSTALL_SPEC replaces the pip argument at every site, and
DANGO_INSTALL_NONINTERACTIVE=1 answers every prompt from named variables, never from a terminal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.factories.installer_fakes import (
    INSTALL_SH,
    base_env,
    run_installer,
    without,
)

pytestmark = pytest.mark.unit

WHEEL = "/work/dist/getdango-1.0.15-py3-none-any.whl"
TEXT = INSTALL_SH.read_text(encoding="utf-8")


# --- the spec override reaches every pip install of Dango -------------------------------------


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("install_dango venv", ["-m", "pip", "install", "{spec}"]),
        ("upgrade_dango venv", ["-m", "pip", "install", "--upgrade", "{spec}", "-q"]),
        ("install_dango_global", ["-m", "pip", "install", "--user", "{spec}"]),
        ("check_conflicts", ["-m", "pip", "install", "--dry-run", "--user", "{spec}"]),
    ],
    ids=["venv-install", "venv-upgrade", "global-install", "global-dry-run"],
)
def test_spec_override_reaches_each_pip_install_site(
    tmp_path: Path, snippet: str, expected: list[str]
) -> None:
    run = run_installer(
        tmp_path, {"DANGO_INSTALL_SPEC": WHEEL}, snippet=snippet, seed_project="venv"
    )
    assert run.proc.returncode == 0, run.output
    want = [a.replace("{spec}", WHEEL) for a in expected]
    assert want in run.pip_installs(), run.pip_installs()


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        ("install_dango venv", ["-m", "pip", "install", "getdango"]),
        ("upgrade_dango venv", ["-m", "pip", "install", "--upgrade", "getdango", "-q"]),
        ("install_dango_global", ["-m", "pip", "install", "--user", "getdango"]),
        ("check_conflicts", ["-m", "pip", "install", "--dry-run", "--user", "getdango"]),
    ],
    ids=["venv-install", "venv-upgrade", "global-install", "global-dry-run"],
)
def test_unset_spec_installs_exactly_getdango(
    tmp_path: Path, snippet: str, expected: list[str]
) -> None:
    run = run_installer(tmp_path, {}, snippet=snippet, seed_project="venv")
    assert run.proc.returncode == 0, run.output
    installs = run.pip_installs()
    assert expected in installs
    others = [c for c in installs if c != expected and c[3:5] != ["--upgrade", "pip"]]
    assert all("--dry-run" in c for c in others), others


def test_every_dango_pip_install_line_uses_the_spec() -> None:
    sites = [
        ln
        for ln in TEXT.splitlines()
        if "pip install" in ln and "echo" not in ln and "--upgrade pip" not in ln
    ]
    assert len(sites) == 4, sites
    for line in sites:
        assert '"$(dango_pip_spec)"' in line or '"$DANGO_PIP_SPEC"' in line, line
        assert not re.search(r"\bgetdango\b", line), line


@pytest.mark.parametrize("flow", ["venv", "global"])
def test_full_run_with_spec_installs_the_spec_only(tmp_path: Path, flow: str) -> None:
    env = base_env(DANGO_INSTALL_SPEC=WHEEL, DANGO_INSTALL_MODE="1" if flow == "venv" else "2")
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    installs = [c for c in run.pip_installs() if c[3:5] != ["--upgrade", "pip"]]
    assert installs
    assert all(WHEEL in c and "getdango" not in c for c in installs), installs


@pytest.mark.parametrize(
    "spec",
    [
        " lead",
        "trail ",
        "a\tb",
        "x;y",
        "$(touch SENTINEL)",
        "`touch SENTINEL`",
        "a&&b",
        "a|b",
        "a>b",
        "a'b",
        '"a"',
        "--index-url=http://example.invalid/simple",
        "-rrequirements.txt",
        "a\nb",
    ],
)
def test_spec_with_spaces_or_metacharacters_is_rejected(tmp_path: Path, spec: str) -> None:
    run = run_installer(tmp_path, base_env(DANGO_INSTALL_SPEC=spec))
    assert run.proc.returncode != 0
    assert "DANGO_INSTALL_SPEC" in run.output
    assert run.pip_installs() == []
    assert not (run.cwd / "venv").exists()
    assert not (run.cwd / "SENTINEL").exists()


@pytest.mark.parametrize(
    "spec",
    [
        "getdango==1.0.15",
        "/tmp/x/dist/getdango-1.0.15-py3-none-any.whl",
        "getdango-1.0.15+local-py3-none-any.whl",
        "C:\\work\\dist\\getdango-1.0.15-py3-none-any.whl",
        "git+https://example.invalid/getdango.git@v1.0.15",
        "/tmp/my dist/getdango-1.0.15-py3-none-any.whl",
        "C:\\Users\\John Smith\\dist\\getdango-1.0.15-py3-none-any.whl",
    ],
)
def test_plain_single_token_specs_are_accepted(tmp_path: Path, spec: str) -> None:
    run = run_installer(tmp_path, base_env(DANGO_INSTALL_SPEC=spec))
    assert run.proc.returncode == 0, run.output
    assert ["-m", "pip", "install", spec] in run.pip_installs()


# --- non-interactive mode ----------------------------------------------------------------------


def test_noninteractive_run_never_touches_the_terminal(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env())
    assert run.proc.returncode == 0, run.output
    assert "/dev/tty" not in run.output
    assert ["-m", "venv", "venv"] in run.calls()


def test_noninteractive_project_flow_runs_init_without_the_wizard(tmp_path: Path) -> None:
    env = base_env(DANGO_INSTALL_SETUP="2", DANGO_INSTALL_PROJECT_NAME="My Analytics!")
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    assert (run.cwd / "my-analytics" / "venv" / "bin" / "activate").is_file()
    assert ["dango", "init", "--skip-wizard"] in run.calls()


def test_noninteractive_custom_venv_location(tmp_path: Path) -> None:
    env = base_env(
        DANGO_INSTALL_SETUP="2",
        DANGO_INSTALL_PROJECT_NAME="p1",
        DANGO_INSTALL_VENV_LOCATION="2",
        DANGO_INSTALL_VENV_PATH="myenv",
    )
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    assert ["-m", "venv", "myenv"] in run.calls()


def test_noninteractive_global_install_adds_path_when_asked(tmp_path: Path) -> None:
    env = base_env(DANGO_INSTALL_MODE="2", DANGO_INSTALL_ADD_TO_PATH="y")
    run = run_installer(tmp_path, env, dango_on_path=False)
    assert run.proc.returncode == 0, run.output
    rc_file = run.home / ".bashrc"
    rc_text = rc_file.read_text("utf-8") if rc_file.exists() else ""
    assert str(run.user_bin) in rc_text or str(run.user_bin) in (
        run.home / ".bash_profile"
    ).read_text("utf-8")


def test_noninteractive_global_conflicts_can_be_accepted(tmp_path: Path) -> None:
    env = base_env(
        DANGO_INSTALL_MODE="2", DANGO_INSTALL_CONFLICTS_CONTINUE="y", FAKE_WOULD_UPGRADE="1"
    )
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    assert ["-m", "pip", "install", "--user", "getdango"] in run.pip_installs()


@pytest.mark.parametrize(("action", "flag"), [("u", "--upgrade"), ("i", "")])
def test_noninteractive_existing_project_with_venv(tmp_path: Path, action: str, flag: str) -> None:
    env = {
        "DANGO_INSTALL_NONINTERACTIVE": "1",
        "DANGO_INSTALL_DOCKER_CONTINUE": "y",
        "DANGO_INSTALL_EXISTING_ACTION": action,
    }
    run = run_installer(tmp_path, env, seed_project="venv")
    assert run.proc.returncode == 0, run.output
    installs = [c for c in run.pip_installs() if c[3:5] != ["--upgrade", "pip"]]
    assert len(installs) == 1 and (flag in installs[0]) == bool(flag), installs


def test_noninteractive_existing_project_without_venv_defaults_to_creating_it(
    tmp_path: Path,
) -> None:
    env = {"DANGO_INSTALL_NONINTERACTIVE": "1", "DANGO_INSTALL_DOCKER_CONTINUE": "y"}
    run = run_installer(tmp_path, env, seed_project="novenv")
    assert run.proc.returncode == 0, run.output
    assert ["-m", "venv", "venv"] in run.calls()


FLOWS: dict[str, tuple[dict[str, str], str]] = {
    "venv-only": (base_env(), ""),
    "global-conflicts": (
        base_env(
            DANGO_INSTALL_MODE="2", DANGO_INSTALL_CONFLICTS_CONTINUE="y", FAKE_WOULD_UPGRADE="1"
        ),
        "",
    ),
    "project": (base_env(DANGO_INSTALL_SETUP="2", DANGO_INSTALL_PROJECT_NAME="p1"), ""),
    "custom-venv": (
        base_env(
            DANGO_INSTALL_SETUP="2",
            DANGO_INSTALL_PROJECT_NAME="p1",
            DANGO_INSTALL_VENV_LOCATION="2",
            DANGO_INSTALL_VENV_PATH="myenv",
        ),
        "",
    ),
    "existing": (
        {
            "DANGO_INSTALL_NONINTERACTIVE": "1",
            "DANGO_INSTALL_DOCKER_CONTINUE": "y",
            "DANGO_INSTALL_EXISTING_ACTION": "u",
        },
        "venv",
    ),
}
REQUIRED = [
    ("venv-only", "DANGO_INSTALL_DOCKER_CONTINUE"),
    ("venv-only", "DANGO_INSTALL_MODE"),
    ("venv-only", "DANGO_INSTALL_SETUP"),
    ("global-conflicts", "DANGO_INSTALL_CONFLICTS_CONTINUE"),
    ("project", "DANGO_INSTALL_PROJECT_NAME"),
    ("custom-venv", "DANGO_INSTALL_VENV_PATH"),
    ("existing", "DANGO_INSTALL_EXISTING_ACTION"),
]


@pytest.mark.parametrize(("flow", "name"), REQUIRED)
def test_missing_answer_variable_fails_naming_it(tmp_path: Path, flow: str, name: str) -> None:
    env, seed = FLOWS[flow]
    run = run_installer(tmp_path, without(env, name), seed_project=seed, timeout=30)
    assert run.proc.returncode != 0
    assert name in run.output
    assert [c for c in run.pip_installs() if "--dry-run" not in c] == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DANGO_INSTALL_MODE", "7"),
        ("DANGO_INSTALL_SETUP", "x"),
        ("DANGO_INSTALL_DOCKER_CONTINUE", "maybe"),
    ],
)
def test_invalid_answer_value_fails_naming_the_variable(
    tmp_path: Path, name: str, value: str
) -> None:
    run = run_installer(tmp_path, base_env(**{name: value}), timeout=30)
    assert run.proc.returncode != 0
    assert name in run.output
    assert run.pip_installs() == []


def test_noninteractive_project_name_that_exists_fails_instead_of_looping(
    tmp_path: Path,
) -> None:
    env = base_env(DANGO_INSTALL_SETUP="2", DANGO_INSTALL_PROJECT_NAME="taken")
    run = run_installer(tmp_path, env, existing_dirs=("taken",), timeout=30)
    assert run.proc.returncode != 0
    assert "DANGO_INSTALL_PROJECT_NAME" in run.output
    assert run.pip_installs() == []


# --- every prompt has a hook --------------------------------------------------------------------

HELPER = re.compile(r"^(read_answer|run_dango_init)\(\) \{\n.*?^\}\n", re.S | re.M)


def _outside_helper() -> str:
    assert "read_answer() {" in TEXT, "install.sh must define a read_answer helper"
    code = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))
    return HELPER.sub("", code + "\n")


def test_no_prompt_reads_the_terminal_outside_the_helpers() -> None:
    rest = _outside_helper()
    assert "/dev/tty" not in rest
    assert not re.search(r"\bread\b", rest), "a prompt must go through read_answer"


def test_every_read_answer_variable_is_documented_in_the_header() -> None:
    header = "\n".join(TEXT.splitlines()[:80])
    names = set(re.findall(r"read_answer \w+ (DANGO_INSTALL_\w+)", TEXT))
    assert len(names) >= 10, names
    for name in names | {"DANGO_INSTALL_SPEC", "DANGO_INSTALL_NONINTERACTIVE"}:
        assert name in header, name


def test_the_eleven_read_sites_are_all_hooked() -> None:
    assert len(re.findall(r"^\s*read_answer \w+ DANGO_INSTALL_\w+", TEXT, re.M)) == 11


# --- non-interactive project names ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["!!!", "-", "--help", " ", "   ", "\t"])
def test_noninteractive_project_name_that_cleans_to_nothing_fails_naming_it(
    tmp_path: Path, name: str
) -> None:
    env = base_env(DANGO_INSTALL_SETUP="2", DANGO_INSTALL_PROJECT_NAME=name)
    run = run_installer(tmp_path, env, timeout=30)
    assert run.proc.returncode != 0
    assert "DANGO_INSTALL_PROJECT_NAME" in run.output
    assert "is not usable as a project directory name" in run.output
    assert run.pip_installs() == []
    assert "illegal option" not in run.output and "OLDPWD" not in run.output


def test_noninteractive_project_name_is_checked_after_the_same_cleanup(tmp_path: Path) -> None:
    env = base_env(DANGO_INSTALL_SETUP="2", DANGO_INSTALL_PROJECT_NAME="my proj")
    run = run_installer(tmp_path, env, existing_dirs=("my-proj",), timeout=30)
    assert run.proc.returncode != 0
    assert "DANGO_INSTALL_PROJECT_NAME='my-proj' already exists" in run.output
    assert not (run.cwd / "my-proj" / "venv").exists()


def test_noninteractive_project_name_is_documented_in_the_header() -> None:
    header = "\n".join(TEXT.splitlines()[:80])
    assert "[a-z0-9][a-z0-9-]*" in header


# --- the two installers share one spec allow-list ------------------------------------------------


def test_spec_allow_list_is_the_same_in_both_installers() -> None:
    ps1 = (INSTALL_SH.parent / "install.ps1").read_text(encoding="utf-8")
    sh_class = re.search(r"allowed='\^(\[[^\]]+\])\+\$'", TEXT)
    ps_class = re.search(r"-notmatch '\^(\[[^\]]+\])\+\\z'", ps1)
    assert sh_class and ps_class
    assert sh_class.group(1) == ps_class.group(1)
    assert " " in sh_class.group(1)
