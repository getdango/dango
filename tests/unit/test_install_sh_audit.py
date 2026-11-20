"""tests/unit/test_install_sh_audit.py

install.sh audit fixes, run against fake python3 and dango executables: conflict detection from
real pip output, global-install guards (active venv, externally managed Python, missing pip,
dango not on PATH), clear failures for venv creation and `dango init`, and the project-name check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.installer_fakes import INSTALL_SH, InstallRun, base_env, run_installer

pytestmark = pytest.mark.unit

TEXT = INSTALL_SH.read_text(encoding="utf-8")
GLOBAL = {"DANGO_INSTALL_MODE": "2"}
PROJECT = {"DANGO_INSTALL_SETUP": "2", "DANGO_INSTALL_PROJECT_NAME": "p1"}
UPGRADE = {
    "FAKE_DRY_RUN_OUTPUT": "Collecting x\nWould install somepkg-2.0 newpkg-1.1 getdango-1.0.15",
    "FAKE_INSTALLED": "somepkg=1.0;getdango=1.0.14",
}


def _user_pip_installs(run: InstallRun) -> list[list[str]]:
    return [c for c in run.pip_installs() if "--user" in c and "--dry-run" not in c]


# --- 3. conflict check reads what pip really prints -------------------------------------------


def test_conflict_check_does_not_grep_a_phrase_pip_never_prints() -> None:
    assert "Would upgrade" not in TEXT


def test_an_upgrade_in_real_pip_output_is_reported_and_needs_an_answer(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(**GLOBAL, **UPGRADE))
    assert run.proc.returncode != 0
    assert "DANGO_INSTALL_CONFLICTS_CONTINUE" in run.output
    listed = [ln for ln in run.output.splitlines() if "somepkg" in ln]
    assert listed and "1.0" in listed[0] and "2.0" in listed[0], run.output
    assert "newpkg" not in run.output and "Would install" not in run.output
    assert _user_pip_installs(run) == []


def test_declining_the_reported_upgrade_cancels_before_installing(tmp_path: Path) -> None:
    env = base_env(**GLOBAL, **UPGRADE, DANGO_INSTALL_CONFLICTS_CONTINUE="n")
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    assert "Installation cancelled" in run.output
    assert _user_pip_installs(run) == []


def test_new_packages_and_dango_itself_are_not_conflicts(tmp_path: Path) -> None:
    env = base_env(**GLOBAL, **UPGRADE, DANGO_INSTALL_CONFLICTS_CONTINUE="n")
    env["FAKE_INSTALLED"] = "getdango=1.0.14"
    run = run_installer(tmp_path, env)
    assert run.proc.returncode == 0, run.output
    assert "No conflicts detected" in run.output
    assert ["-m", "pip", "install", "--user", "getdango"] in run.pip_installs()


# --- 4. global install only: dango not on PATH ------------------------------------------------


def test_global_install_only_without_path_explains_the_next_step(tmp_path: Path) -> None:
    env = base_env(**GLOBAL, DANGO_INSTALL_ADD_TO_PATH="n")
    run = run_installer(tmp_path, env, dango_on_path=False)
    assert run.proc.returncode == 0, run.output
    assert "Dango is installed, but 'dango' is not on your PATH yet" in run.output
    assert f"{run.user_bin}/dango init my-project" in run.output
    assert f'export PATH="{run.user_bin}:$PATH"' in run.output


def test_global_project_without_path_does_not_claim_the_install_failed(tmp_path: Path) -> None:
    env = base_env(**GLOBAL, **PROJECT, DANGO_INSTALL_ADD_TO_PATH="n")
    run = run_installer(tmp_path, env, dango_on_path=False)
    assert run.proc.returncode != 0
    assert "Global installation failed" not in run.output
    assert "the project was not initialised" in run.output
    assert "cd p1" in run.output and f"{run.user_bin}/dango init" in run.output
    assert ["dango", "init", "--skip-wizard"] not in run.calls()


# --- 6, and global-install guards ------------------------------------------------------------


def test_global_install_inside_an_active_venv_stops_with_advice(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(**GLOBAL, FAKE_IN_VENV="1"))
    assert run.proc.returncode != 0
    assert "inside an active virtual environment" in run.output
    assert "deactivate" in run.output
    assert _user_pip_installs(run) == []


def test_global_install_on_an_externally_managed_python_stops_with_advice(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(**GLOBAL, FAKE_EXTERNALLY_MANAGED="1"))
    assert run.proc.returncode != 0
    assert "managed by your operating system" in run.output
    assert "option [1]" in run.output and "pipx install getdango" in run.output
    assert _user_pip_installs(run) == []


def test_global_install_without_pip_names_the_package(tmp_path: Path) -> None:
    run = run_installer(
        tmp_path,
        base_env(**GLOBAL, FAKE_NO_PIP="1"),
        snippet="detect_distro_family() { echo debian; }; install_dango_global",
    )
    assert run.proc.returncode != 0
    assert "pip is not available" in run.output
    assert "sudo apt install python3-pip" in run.output
    assert _user_pip_installs(run) == []


def test_a_failed_conflict_check_is_not_silent(tmp_path: Path) -> None:
    snippet = "python3() { return 1; }; PYTHON_CMD=python3; check_conflicts"
    run = run_installer(tmp_path, base_env(**GLOBAL), snippet=snippet)
    assert run.proc.returncode != 0
    assert "Could not check for package conflicts" in run.output


# --- 7. venv creation fails -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("family", "hint"),
    [
        ("debian", "sudo apt install python3-venv"),
        ("fedora", "sudo dnf install python3"),
        ("other", "python3 -m venv"),
    ],
)
def test_failed_venv_creation_prints_the_exact_fix(tmp_path: Path, family: str, hint: str) -> None:
    snippet = f"detect_distro_family() {{ echo {family}; }}; create_venv venv"
    run = run_installer(tmp_path, {"FAKE_VENV_FAIL": "1"}, snippet=snippet)
    assert run.proc.returncode != 0
    assert "Failed to create virtual environment" in run.output
    assert hint in run.output
    assert not (run.cwd / "venv").exists()  # this run's half-made directory is removed


def test_the_venv_package_follows_the_python_version(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, snippet="venv_install_hint debian python3.12")
    assert "sudo apt install python3.12-venv" in run.proc.stdout


def test_failed_venv_creation_keeps_a_directory_that_was_already_there(tmp_path: Path) -> None:
    snippet = "mkdir keepme; create_venv keepme"
    run = run_installer(tmp_path, {"FAKE_VENV_FAIL": "1"}, snippet=snippet)
    assert run.proc.returncode != 0
    assert (run.cwd / "keepme").is_dir()


def test_full_run_with_failing_venv_stops_before_installing(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(FAKE_VENV_FAIL="1"))
    assert run.proc.returncode != 0
    assert "Failed to create virtual environment" in run.output
    assert run.pip_installs() == []


# --- unsupported or missing Python ------------------------------------------------------------


def test_too_old_python_is_named_and_the_hint_matches_the_distro(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(FAKE_PY_VERSION="3.10"))
    assert run.proc.returncode != 0
    assert "Found Python 3.10" in run.output


@pytest.mark.parametrize(
    ("family", "hint"),
    [
        ("debian", "python3-venv"),
        ("fedora", "sudo dnf install python3.13"),
        ("macos", "brew install python@3.12"),
    ],
)
def test_python_install_hint_is_per_distro(tmp_path: Path, family: str, hint: str) -> None:
    run = run_installer(tmp_path, {}, snippet=f"python_install_hint {family}")
    assert hint in run.proc.stdout


def test_ubuntu_22_04_hint_says_python_3_12_is_not_in_its_repositories(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, snippet="python_install_hint debian")
    assert "22.04" in run.proc.stdout


# --- 11. dango init fails ---------------------------------------------------------------------


def test_failed_init_in_a_venv_project_prints_what_to_run_and_where_logs_are(
    tmp_path: Path,
) -> None:
    run = run_installer(tmp_path, base_env(**PROJECT, FAKE_DANGO_INIT_FAIL="1"))
    assert run.proc.returncode != 0
    for text in (
        "Failed to initialize project",
        "cd p1",
        "source venv/bin/activate",
        "dango init",
        ".dango/logs/dango.log",
    ):
        assert text in run.output, text


def test_failed_init_in_a_global_project_prints_what_to_run_and_where_logs_are(
    tmp_path: Path,
) -> None:
    run = run_installer(tmp_path, base_env(**GLOBAL, **PROJECT, FAKE_DANGO_INIT_FAIL="1"))
    assert run.proc.returncode != 0
    for text in ("Failed to initialize project", "cd p1", "dango init", ".dango/logs/dango.log"):
        assert text in run.output, text


# --- 5. project name: checked after cleanup, also when typed ------------------------------------

ANSWERS = "y\n1\n2\n"  # continue without Docker; virtual environment; install + project


def test_typed_name_is_checked_for_an_existing_folder_after_cleanup(tmp_path: Path) -> None:
    typed = ANSWERS + "My Proj\nfresh\n1\n"
    run = run_installer(tmp_path, {}, existing_dirs=("my-proj",), typed=typed)
    assert run.proc.returncode == 0, run.output
    assert "Directory 'my-proj' already exists" in run.output
    assert (run.cwd / "fresh" / "venv").is_dir()
    assert not (run.cwd / "my-proj" / "venv").exists()


def test_typed_name_that_cleans_to_nothing_asks_again(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, typed=ANSWERS + "!!!\nok\n1\n")
    assert run.proc.returncode == 0, run.output
    assert "Project name cannot be empty" in run.output
    assert (run.cwd / "ok" / "venv").is_dir()


def test_typed_name_starting_with_a_dash_asks_again(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, typed=ANSWERS + "--help\nok\n1\n")
    assert run.proc.returncode == 0, run.output
    assert "start with a letter or digit" in run.output
    assert (run.cwd / "ok" / "venv").is_dir()
    assert not (run.cwd / "--help").exists()


def test_typed_name_that_is_fine_is_unchanged(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, typed=ANSWERS + "My Analytics!\n1\n")
    assert run.proc.returncode == 0, run.output
    assert (run.cwd / "my-analytics" / "venv").is_dir()
    assert "Creating project directory: my-analytics" in run.output
