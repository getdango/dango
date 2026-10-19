"""tests/unit/test_compose_helper_install.py

Install scripts, sudoers text, budgets and the root-side command wrapper for the compose helper.
"""

from __future__ import annotations

import subprocess

import pytest

from dango.platform.cloud import compose_helper as ch

pytestmark = pytest.mark.unit


def test_sudoers_text_is_exact() -> None:
    assert ch.SUDOERS_TEXT == (
        "# Installed by Dango. The dango account may run only the root-owned compose helper;"
        " the helper checks every argument.\n"
        "dango ALL=(root) NOPASSWD: /usr/local/sbin/dango-compose *\n"
    )
    assert "SETENV" not in ch.SUDOERS_TEXT


def test_sudoers_is_validated_before_it_is_moved_into_place() -> None:
    script = ch.install_sudoers_script()
    tmp = f"{ch.SUDOERS_PATH}.tmp"
    order = [
        script.index(f"cat >{tmp}"),
        script.index(f"chmod 0440 {tmp}"),
        script.index(f"visudo -cf {tmp}"),
        script.index(f"mv -f {tmp} {ch.SUDOERS_PATH}"),
        script.index("visudo -c >/dev/null"),
        script.index("sudo -l -U dango"),
    ]
    assert order == sorted(order)
    assert f"rm -f {tmp}" in script


def test_helper_is_owned_and_made_executable_before_it_is_moved_into_place() -> None:
    script = ch.install_helper_script()
    tmp = f"{ch.HELPER_PATH}.tmp"
    assert (
        script.index("chown root:root")
        < script.index("chmod 0755")
        < script.index(f"mv -f {tmp} {ch.HELPER_PATH}")
    )
    assert ch.helper_script() in script


def test_layout_directories_are_root_owned_and_metabase_is_private() -> None:
    script = ch.install_dirs_script()
    assert f"install -d -o root -g root -m 0700 {ch.METABASE_DIR} {ch.PLUGINS_DIR}" in script
    assert (
        f"install -d -o root -g root -m 0755 {ch.SRV_DIR} {ch.COMPOSE_DIR} {ch.BUILD_DIR}" in script
    )
    assert "is a symlink" in script


def test_generated_install_script_is_valid_sh_syntax() -> None:
    result = subprocess.run(
        ["sh", "-n"], input=ch.install_script(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        ["sh", "-n"], input=ch.helper_script(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_helper_text_has_no_unreplaced_placeholders_and_a_single_shebang() -> None:
    text = ch.helper_script()
    assert not any(token in text for token in ("@BUDGET_CASES@", "@SUDO_WAIT@", "@ROOT_WAIT@"))
    assert text.startswith("#!/bin/sh\n") and text.count("#!/bin/sh") == 1


def test_every_verb_has_a_budget_and_callers_get_a_longer_timeout() -> None:
    for verb, seconds in ch.VERB_BUDGET.items():
        wait = ch.SUDO_LOCK_WAIT if verb in ch.LOCKED_VERBS else 0
        assert (
            ch.min_caller_timeout(verb) == wait + seconds + ch.CALLER_MARGIN > wait + seconds + 10
        )
    assert ch.min_caller_timeout("stop") == 30 + 45 + 15 <= 90
    assert ch.min_caller_timeout("start") == 30 + 90 + 15 <= 150
    assert ch.SUDO_LOCK_WAIT <= 30


def test_server_compose_cmd_uses_the_helper_only_with_the_marker() -> None:
    cmd = ch.server_compose_cmd("stop", "metabase", legacy="docker compose stop metabase")
    assert cmd == (
        f"if [ -f {ch.MARKER} ] && [ ! -L {ch.MARKER} ]; then {ch.HELPER_PATH} stop metabase;"
        " else docker compose stop metabase; fi"
    )
    with pytest.raises(ValueError):
        ch.server_compose_cmd("rm", legacy="true")


def test_server_compose_cmd_quotes_arguments_and_falls_back_without_the_marker() -> None:
    cmd = ch.server_compose_cmd("reset-password", "a b@c.d", legacy="echo legacy")
    assert "'a b@c.d'" in cmd
    ran = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, check=False)
    assert ran.stdout.strip() == "legacy"  # no marker on this machine: the legacy branch runs
