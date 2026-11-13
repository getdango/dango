"""tests/unit/test_server_python.py

Server venv interpreter scripts: host detection (real dash runs), pinned install, constants.
"""

from __future__ import annotations

import ast
import getpass
import importlib
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
CLOUD = REPO / "dango" / "platform" / "cloud"


def _sps() -> ModuleType:
    return importlib.import_module("dango.platform.cloud.server_python_scripts")


# ---------------------------------------------------------------------------
# Rendered scripts are valid POSIX sh
# ---------------------------------------------------------------------------


def _dash() -> str:
    path = shutil.which("dash")
    assert path, "dash is needed to check the rendered scripts"
    return path


@pytest.mark.unit
class TestScriptsParse:
    @pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
    def test_scripts_pass_dash_n(self, arch: str) -> None:
        sps = _sps()
        build = sps.PYTHON_BUILDS[arch]
        for script in (
            sps.host_python_script(),
            sps.installed_check_script(build.sha256),
            sps.install_script(build),
        ):
            result = subprocess.run([_dash(), "-n", "-c", script], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr

    def test_venv_trust_scripts_still_parse(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        for script in (ro.VENV_TAINT_PROBE, ro.build_verify_script()):
            result = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
            assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Host interpreter detection: real dash runs against fake interpreters
# ---------------------------------------------------------------------------


def _fake(path: Path, output: str, *, code: int = 0, mode: int = 0o755) -> Path:
    """A fake interpreter that leaves a trace when run and prints *output*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    trace = path.parent / f".ran-{path.name}"
    path.write_text(f'#!/bin/sh\ntouch "{trace}"\necho "{output}"\nexit {code}\n')
    path.chmod(mode)
    return trace


class _Host:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve() / "host"
        self.usr = self.root / "usr"
        self.bin = self.root / "pathbin"
        self.bin.mkdir(parents=True)
        self.usr.mkdir()

    def run(
        self, names: tuple[str, ...] = ("python3.13", "python3.12", "python3.11", "python3")
    ) -> str:
        script = _sps().host_python_script(
            names=names,
            owner=getpass.getuser(),
            allowed=str(self.usr),
            top=str(self.root),
        )
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root)}
        result = subprocess.run(
            [_dash(), "-c", script], capture_output=True, text=True, env=env, check=False
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()


@pytest.mark.unit
class TestHostDetection:
    def test_good_interpreter_is_reported(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        _fake(host.bin / "python3.12", "3.12.3.final")
        (host.bin / "python3.12").unlink()
        real = host.usr / "bin" / "python3.12"
        _fake(real, "3.12.3.final")
        os.symlink(real, host.bin / "python3.12")
        assert host.run() == str(real)  # the link is resolved; the real path is reported

    def test_wrong_version_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.10"
        _fake(real, "3.10.12.final")
        os.symlink(real, host.bin / "python3")
        assert host.run() == ""

    def test_pre_release_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.11"
        _fake(real, "3.11.0.candidate")
        os.symlink(real, host.bin / "python3.11")
        assert host.run() == ""

    def test_too_new_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.14"
        _fake(real, "3.14.0.final")
        os.symlink(real, host.bin / "python3")
        assert host.run() == ""

    def test_missing_venv_or_ensurepip_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.12"
        _fake(real, "ModuleNotFoundError: No module named 'ensurepip'", code=1)
        os.symlink(real, host.bin / "python3.12")
        assert host.run() == ""

    def test_in_range_python_without_venv_is_named_but_not_chosen(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.12"
        real.parent.mkdir(parents=True)
        real.write_text(
            '#!/bin/sh\ncase "$*" in *ensurepip*) echo "No module named ensurepip" >&2; exit 1;; '
            'esac\necho "3.12.3.final"\n'
        )
        real.chmod(0o755)
        os.symlink(real, host.bin / "python3")
        assert host.run() == f"novenv {real}"

    def test_writable_parent_is_passed_over_and_never_run(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.12"
        trace = _fake(real, "3.12.3.final")
        os.symlink(real, host.bin / "python3.12")
        (host.usr / "bin").chmod(0o777)
        try:
            assert host.run() == ""
        finally:
            (host.usr / "bin").chmod(0o755)
        assert not trace.exists()  # judged as data first: an unsafe candidate is never run

    def test_writable_file_is_passed_over_and_never_run(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.usr / "bin" / "python3.12"
        trace = _fake(real, "3.12.3.final", mode=0o777)
        os.symlink(real, host.bin / "python3.12")
        assert host.run() == ""
        assert not trace.exists()

    def test_symlink_out_of_the_allowed_tree_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        real = host.root / "tmp" / "python3.12"
        trace = _fake(real, "3.12.3.final")
        os.symlink(real, host.bin / "python3.12")
        assert host.run() == ""
        assert not trace.exists()

    def test_under_tmp_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        trace = _fake(host.root / "tmp" / "bin" / "python3", "3.12.3.final")
        os.symlink(host.root / "tmp" / "bin" / "python3", host.bin / "python3")
        assert host.run() == ""
        assert not trace.exists()

    def test_outside_usr_is_passed_over(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        trace = _fake(host.bin / "python3.12", "3.12.3.final")
        assert host.run() == ""  # the venv trust check accepts links into /usr only
        assert not trace.exists()

    def test_first_good_name_wins_and_bad_ones_fall_through(self, tmp_path: Path) -> None:
        host = _Host(tmp_path)
        bad = host.usr / "bin" / "python3.13"
        _fake(bad, "3.13.1.final", code=1)
        good = host.usr / "bin" / "python3.11"
        _fake(good, "3.11.9.final")
        better = host.usr / "bin" / "python3.12"
        _fake(better, "3.12.3.final")
        for path in (bad, good, better):
            os.symlink(path, host.bin / path.name)
        assert host.run() == str(better)

    @pytest.mark.parametrize("where", ["home/u/bin", "srv/dango/venv/bin", "var/tmp"])
    def test_other_trees_are_passed_over(self, tmp_path: Path, where: str) -> None:
        host = _Host(tmp_path)
        trace = _fake(host.root / where / "python3.12", "3.12.3.final")
        os.symlink(host.root / where / "python3.12", host.bin / "python3.12")
        assert host.run() == ""
        assert not trace.exists()

    def test_production_script_requires_root_and_usr(self) -> None:
        script = _sps().host_python_script()
        assert "! -user root" in script
        assert 'case "$r" in /usr/*) ;; *) continue ;; esac' in script
        assert script.index("! -user root") < script.index('"$r" -I -c')


# ---------------------------------------------------------------------------
# Install script: GNU-only, so checked as text (never executed on macOS)
# ---------------------------------------------------------------------------


def _install(arch: str = "x86_64") -> tuple[str, Any]:
    sps = _sps()
    build = sps.PYTHON_BUILDS[arch]
    return sps.install_script(build), build


@pytest.mark.unit
class TestInstallScript:
    @pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
    def test_download_is_https_only_bounded_and_pinned(self, arch: str) -> None:
        script, build = _install(arch)
        curl = next(line for line in script.splitlines() if "curl --proto" in line)
        for flag in ("--proto '=https'", "--proto-redir '=https'", "--tlsv1.2", "--fail"):
            assert flag in curl
        assert "--max-redirs 3" in curl
        assert f"--max-time {_sps().DOWNLOAD_MAX_TIME}" in curl
        assert f"| head -c {build.size + 1} >" in curl  # unknown-length bodies are capped too
        assert f"--max-filesize {build.size + _sps().SIZE_MARGIN}" in curl
        assert build.url in curl
        assert build.url.startswith("https://github.com/astral-sh/python-build-standalone/")

    def test_size_and_checksum_are_checked_before_anything_is_extracted(self) -> None:
        script, build = _install()
        order = [
            "curl --proto",
            f'[ "$size" = {build.size} ]',
            "sha256sum -c --status",
            "tar -tzf",
            "tar -xzf",
            "chown -R root:root",
            '> "$work"/x/python/.dango-python.sha256',
            "mv -T /opt/dango/python/cpython-",
            'mv -T "$work"/x/python',
        ]
        positions = [script.index(item) for item in order]
        assert positions == sorted(positions), list(zip(order, positions, strict=True))
        assert re.search(r"exit 11; \}", script) and re.search(r"exit 12; \}", script)
        assert build.sha256 in script

    def test_archive_is_never_piped_into_tar(self) -> None:
        script, _build = _install()
        assert not re.search(r"\|\s*tar\b", script)
        assert '-C "$work"/x --no-same-owner --no-same-permissions' in script

    def test_archive_entries_are_restricted(self) -> None:
        script, _build = _install()
        assert "grep -v '^python/'" in script  # everything under python/ (no absolute names)
        assert "grep -E '(^|/)[.][.](/|$)'" in script  # no .. component
        assert "grep -v '^[-dl]$'" in script  # files, directories and links only
        after = script[script.index("tar -xzf") :]
        assert "! -type f ! -type d ! -type l -print" in after  # no devices/fifos
        assert "-type f -links +1 -print" in after  # no hard links
        assert 'case "$t" in "$work"/x/python/*) ;; *) printf' in after  # links stay inside

    def test_opt_dango_must_be_root_owned_and_not_a_link(self) -> None:
        script, _build = _install()
        assert "for d in / /opt /opt/dango /opt/dango/python; do" in script
        assert 'if [ -L "$d" ]; then' in script
        assert "! -user root -o -perm -020 -o -perm -002" in script
        assert script.index("for d in / /opt") < script.index("install -d -o root -g root -m 0755")

    def test_staging_is_fresh_private_and_stale_runs_are_cleaned_first(self) -> None:
        script, _build = _install()
        lock = script.index(f"flock -w {_sps().LOCK_WAIT} 9")
        stale = script.index("for s in /opt/dango/python/*.tmp; do")
        mktemp = script.index("mktemp -d /opt/dango/python/stage.XXXXXXXXXX.tmp")
        assert lock < stale < mktemp < script.index("curl --proto")  # cleanup only under the lock
        assert "exec 9>/opt/dango/python/.install.lock" in script
        assert script.startswith("set -u\numask 022\n")

    def test_interrupted_run_leaves_the_installed_tree_until_the_new_one_is_verified(self) -> None:
        script, _build = _install()
        replaced = script.index("mv -T /opt/dango/python/cpython-")
        for step in ("sha256sum", "tar -xzf", "-links +1", "chmod -R go-w", ".dango-python.sha256"):
            assert script.index(step) < replaced
        every_failure = re.findall(r"\|\| \{ rm -rf -- \"\$work\"; echo", script)
        assert len(every_failure) >= 6  # each failure removes its own staging directory


# ---------------------------------------------------------------------------
# Download step: real dash runs with a fake curl (the rest of the script is GNU-only)
# ---------------------------------------------------------------------------


def _download(tmp_path: Path, curl_body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the download step against a fake ``curl`` (pinned size 1000 bytes)."""
    sps = _sps()
    build = sps.PythonBuild(url="https://example.invalid/python.tar.gz", size=1000, sha256="0" * 64)
    bin_dir, work = tmp_path / "bin", tmp_path / "work"
    bin_dir.mkdir()
    work.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{tmp_path}/args"\n{curl_body}\n')
    curl.chmod(0o755)
    script = (
        f"work='{work}'\n"
        + sps.download_script(build)
        + 'echo "ok $(wc -c < "$work"/python.tar.gz | tr -d " ")"\n'
    )
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(
        [_dash(), "-c", script], capture_output=True, text=True, env=env, check=False, timeout=60
    )
    return result, work


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""


@pytest.mark.unit
class TestDownload:
    def test_exact_size_download_succeeds(self, tmp_path: Path) -> None:
        result, _work = _download(tmp_path, "head -c 1000 /dev/zero; exit 0")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "ok 1000"
        args = (tmp_path / "args").read_text().splitlines()
        assert "--max-filesize" in args and str(1000 + _sps().SIZE_MARGIN) in args
        assert "--max-time" in args and str(_sps().DOWNLOAD_MAX_TIME) in args
        assert "-o" not in args and args[-1] == "https://example.invalid/python.tar.gz"

    @pytest.mark.parametrize(
        ("code", "message"),
        [
            (6, "curl: (6) Could not resolve host: github.com"),
            (22, "curl: (22) The requested URL returned error: 404"),
            (35, "curl: (35) OpenSSL SSL_connect: Connection reset by peer"),
            (28, "curl: (28) Operation timed out after 600000 milliseconds"),
        ],
    )
    def test_failure_keeps_curls_own_error_and_exit_code(
        self, tmp_path: Path, code: int, message: str
    ) -> None:
        result, work = _download(tmp_path, f'echo "{message}" >&2; exit {code}')
        assert result.returncode == _sps().EXIT_DOWNLOAD
        last = _last_line(result.stderr)  # what the refusal message shows
        assert message in last and f"curl exit {code}" in last, result.stderr
        assert not work.exists()

    def test_max_filesize_is_a_size_refusal(self, tmp_path: Path) -> None:
        result, work = _download(
            tmp_path, 'echo "curl: (63) Maximum file size exceeded" >&2; exit 63'
        )
        assert result.returncode == _sps().EXIT_SIZE
        last = _last_line(result.stderr)
        assert "Maximum file size exceeded" in last and "curl exit 63" in last, result.stderr
        assert not work.exists()

    def test_unknown_length_body_is_capped_at_one_byte_over_the_pinned_size(
        self, tmp_path: Path
    ) -> None:
        # curl 7.81 ignores --max-filesize without a Content-Length: only the cap stops it
        result, work = _download(tmp_path, "head -c 5000 /dev/zero; exit 0")
        assert result.returncode == _sps().EXIT_SIZE
        assert "size 1001, expected 1000" in _last_line(result.stderr), result.stderr
        assert not work.exists()

    def test_endless_body_stops_at_the_cap(self, tmp_path: Path) -> None:
        result, work = _download(tmp_path, "cat /dev/zero; exit $?")
        assert result.returncode == _sps().EXIT_SIZE, result.stderr
        assert "1000 bytes" in _last_line(result.stderr), result.stderr
        assert not work.exists()

    def test_lock_wait_and_download_fit_inside_the_ssh_timeout(self) -> None:
        from dango.platform.cloud import server_python as sp

        sps = _sps()
        script = sps.install_script(sps.PYTHON_BUILDS["x86_64"])
        assert f"flock -w {sps.LOCK_WAIT} 9" in script
        assert f"--max-time {sps.DOWNLOAD_MAX_TIME} " in script
        assert (sps.LOCK_WAIT, sps.DOWNLOAD_MAX_TIME, sp.INSTALL_STEP_TIMEOUT) == (120, 600, 1200)
        # the script prints nothing until it ends: the read timeout must cover the slowest run
        assert sps.LOCK_WAIT + sps.DOWNLOAD_MAX_TIME + 300 <= sp.INSTALL_STEP_TIMEOUT


# ---------------------------------------------------------------------------
# Pinned values, package metadata and system-python scripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstants:
    def test_accepted_range_equals_requires_python(self) -> None:
        from packaging.specifiers import SpecifierSet

        sps = _sps()
        spec = SpecifierSet(
            tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["requires-python"]
        )
        for minor in range(6, 20):
            in_module = sps.MIN_PYTHON <= (3, minor) < sps.MAX_PYTHON
            assert spec.contains(f"3.{minor}.0") is in_module, minor
        assert sps.accepted_versions() == [f"3.{m}.*.final" for m in range(11, 14)]
        assert sps.SUPPORTED == ", ".join(f"3.{m}" for m in range(11, 13)) + " or 3.13"

    def test_pinned_builds_are_complete(self) -> None:
        sps = _sps()
        assert set(sps.PYTHON_BUILDS) == {"x86_64", "aarch64"}
        for arch, build in sps.PYTHON_BUILDS.items():
            assert re.fullmatch(r"[0-9a-f]{64}", build.sha256)
            assert build.size > 10_000_000
            assert (
                f"cpython-{sps.PYTHON_VERSION}%2B{sps.PBS_RELEASE}-{arch}-unknown-linux-gnu-install_only.tar.gz"
                in build.url
            )
        assert sps.PYTHON_BIN.startswith(sps.PYTHON_HOME + "/")

    def test_system_python_scripts_stay_valid_for_python_3_10(self) -> None:
        from dango.platform.cloud import runner_isolation

        users = sorted(
            p.name
            for p in CLOUD.glob("*.py")
            if re.search(r"\bpython3 (-[A-Za-z]+ )*-c\b", p.read_text())
        )
        assert users == ["runner_isolation.py"], users  # a new one must be added below
        for code in (runner_isolation.record_python(), _sps().PROBE):
            ast.parse(code, feature_version=(3, 10))
        for code in (_sps().PROBE, _sps().VERSION_PROBE):  # run by any candidate interpreter
            ast.parse(code, feature_version=(3, 6))


@pytest.mark.unit
class TestVenvLinkCheck:
    def test_links_into_the_installed_interpreter_are_judged_by_its_whole_tree(self) -> None:
        from dango.platform.cloud import runtime_ownership as ro

        for script in (ro.VENV_TAINT_PROBE, ro.build_verify_script()):
            assert (
                "(/opt/dango/python/*) g=${t#/opt/dango/python/}; g=/opt/dango/python/${g%%/*};"
                in script
            )
            assert 'find / /opt /opt/dango /opt/dango/python "$g" -maxdepth 0' in script
            assert 'find "$g" \\( ! -user root -o' in script
            # other targets outside the venv and /usr are still reported
            assert '(*) echo "$l -> $t"; continue;;' in script
