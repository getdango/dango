"""tests/unit/test_install_sh_review.py

install.sh review fixes: advice that works per platform (pip, Python too new or too old), a pip too
old for --dry-run, a failing pip upgrade, fish PATH syntax, a dangling symlink at the venv path,
and set -e staying active inside install_dango_global.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.installer_fakes import INSTALL_SH, base_env, run_installer

pytestmark = pytest.mark.unit

TEXT = INSTALL_SH.read_text(encoding="utf-8")
GLOBAL = {"DANGO_INSTALL_MODE": "2"}


def _user_installs(run_calls: list[list[str]]) -> list[list[str]]:
    return [c for c in run_calls if c[:4] == ["-m", "pip", "install", "--user"]]


# --- every upgraded package is listed, not just the first ------------------------------------------


def test_every_upgraded_package_is_listed(tmp_path: Path) -> None:
    env = base_env(
        **GLOBAL,
        FAKE_DRY_RUN_OUTPUT="Would install somepkg-2.0 other-3.1 fresh-1.0 getdango-1.0.15",
        FAKE_INSTALLED="somepkg=1.0;other=3.0",
    )
    run = run_installer(tmp_path, env)
    assert "DANGO_INSTALL_CONFLICTS_CONTINUE" in run.output
    assert "somepkg: 1.0" in run.output and "other: 3.0" in run.output
    assert "fresh" not in run.output.replace("(non-interactive)", "")


# --- pip advice that works on Debian and Ubuntu (ensurepip is disabled there) --------------------


def test_pip_hint_for_a_versioned_python_on_debian_is_the_distro_package(tmp_path: Path) -> None:
    snippet = "detect_distro_family() { echo debian; }; PYTHON_CMD=python3.11; install_dango_global"
    run = run_installer(tmp_path, {}, snippet=snippet)
    assert run.proc.returncode != 0
    assert "sudo apt install python3-pip" in run.output
    assert "ensurepip" not in run.output  # ensurepip is disabled for the system Python there


# --- a pip too old for --dry-run: go on without the check, do not claim the install would fail -------------


def test_pip_too_old_for_dry_run_goes_on_without_the_conflict_check(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(**GLOBAL, FAKE_PIP_NO_DRYRUN="1"))
    assert run.proc.returncode == 0, run.output
    assert "too old" in run.output and "22.2" in run.output
    assert "would fail the same way" not in run.output
    assert _user_installs(run.pip_installs()) == [["-m", "pip", "install", "--user", "getdango"]]


# --- Python too new or too old and no distribution package: not a circular hint ------------------


@pytest.mark.parametrize("family", ["debian", "other"])
def test_python_hint_has_a_route_when_the_repositories_have_no_supported_python(
    tmp_path: Path, family: str
) -> None:
    run = run_installer(tmp_path, {}, snippet=f"python_install_hint {family}")
    assert "python.org" in run.proc.stdout
    assert "uv python install 3.13" in run.proc.stdout


# --- a failing pip upgrade is explained ------------------------------------------------------------


def test_failing_pip_upgrade_warns_and_installs_dango_anyway(tmp_path: Path) -> None:
    run = run_installer(tmp_path, base_env(FAKE_PIP_FAIL="pip"))
    assert run.proc.returncode == 0, run.output
    assert "Could not upgrade pip" in run.output
    assert ["-m", "pip", "install", "getdango"] in run.pip_installs()


def test_failing_dango_upgrade_says_so_instead_of_exiting_silently(tmp_path: Path) -> None:
    env = {
        "DANGO_INSTALL_NONINTERACTIVE": "1",
        "DANGO_INSTALL_DOCKER_CONTINUE": "y",
        "DANGO_INSTALL_EXISTING_ACTION": "u",
        "FAKE_PIP_FAIL": "dango",
    }
    run = run_installer(tmp_path, env, seed_project="venv")
    assert run.proc.returncode != 0
    assert "Failed to upgrade Dango" in run.output


# --- fish gets fish syntax ---------------------------------------------------------------------------


def test_fish_gets_fish_add_path_not_a_bash_export(tmp_path: Path) -> None:
    env = base_env(**GLOBAL, DANGO_INSTALL_ADD_TO_PATH="y", SHELL="/usr/bin/fish")
    run = run_installer(tmp_path, env, dango_on_path=False)
    assert run.proc.returncode == 0, run.output
    config = run.home / ".config" / "fish" / "config.fish"
    assert config.is_file()
    assert f'fish_add_path "{run.user_bin}"' in config.read_text("utf-8")
    assert "export PATH" not in config.read_text("utf-8")
    assert "export PATH" not in run.output


def test_a_shell_config_that_cannot_be_written_is_reported_not_claimed_as_added(
    tmp_path: Path,
) -> None:
    env = base_env(**GLOBAL, DANGO_INSTALL_ADD_TO_PATH="y", HOME=str(tmp_path / "proj"))
    run = run_installer(
        tmp_path, env, dango_on_path=False, existing_dirs=(".bashrc", ".bash_profile")
    )
    assert run.proc.returncode == 0, run.output
    assert "Could not write to" in run.output
    assert "Added to" not in run.output
    assert "Dango is installed, but 'dango' is not on your PATH yet" in run.output


# --- a dangling symlink at the venv path is never deleted --------------------------------------------


def test_failed_venv_creation_keeps_a_dangling_symlink_at_the_path(tmp_path: Path) -> None:
    snippet = "ln -s /no/such/target dangling; create_venv dangling"
    run = run_installer(tmp_path, {"FAKE_VENV_FAIL": "1"}, snippet=snippet)
    assert run.proc.returncode != 0
    assert (run.cwd / "dangling").is_symlink()


# --- set -e stays active inside install_dango_global; the result is a variable, not a status -------


def test_install_dango_global_is_not_called_in_an_if_condition() -> None:
    assert "if ! install_dango_global" not in TEXT
    assert "if install_dango_global" not in TEXT
    assert "GLOBAL_ON_PATH" in TEXT


# --- shellcheck notes added by the first round are gone ----------------------------------------------


def test_no_word_splitting_set_and_no_sourced_os_release() -> None:
    assert "set -- $installed" not in TEXT
    assert ". /etc/os-release" not in TEXT
