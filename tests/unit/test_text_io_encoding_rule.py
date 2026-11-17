"""tests/unit/test_text_io_encoding_rule.py

Repo-wide rule: local code names UTF-8 on every text open and text subprocess, and writes LF.
"""

# Server-only (platform/cloud, runtime) and vendored (ingestion/dlt_sources) code is excluded:
# it only runs on Linux servers or is third-party. See tests/factories/text_io_scan.py.
import subprocess

import pytest

from tests.factories.text_io_scan import EXCLUDED_PREFIXES, REPO_ROOT, scan_tree, scanned_files

pytestmark = pytest.mark.unit


def test_scan_covers_local_code_and_only_the_documented_exclusions() -> None:
    rels = {p.relative_to(REPO_ROOT).as_posix() for p in scanned_files()}
    assert len(rels) > 250
    assert not [r for r in rels if r.startswith(EXCLUDED_PREFIXES)]
    for prefix in EXCLUDED_PREFIXES:
        assert (REPO_ROOT / prefix).exists(), f"stale exclusion {prefix}"
    assert {"dango/web/app.py", "dango/platform/docker.py", "dango/cli/init.py"} <= rels


def test_text_io_names_encoding_and_lf_newline() -> None:
    assert scan_tree() == []


# `.gitattributes`: templates and generated-script sources must check out with LF on Windows.
@pytest.mark.parametrize(
    "path",
    [
        "dango/templates/entrypoint.sh",
        "dango/templates/docker-compose.yml.j2",
        "dango/templates/Dockerfile.metabase",
        "dango/templates/nginx.conf.j2",
        "dango/templates/CLAUDE.md",
        "install.sh",
        "scripts/run_tests.sh",
        ".github/workflows/ci.yml",
        "pyproject.toml",
    ],
)
def test_gitattributes_forces_lf_checkout(path: str) -> None:
    assert (REPO_ROOT / ".gitattributes").is_file()
    out = subprocess.run(
        ["git", "check-attr", "eol", "--", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    assert out.strip().endswith("eol: lf"), out
