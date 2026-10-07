"""tests/unit/test_resolve_metabase_url.py

Resolver precedence for the host-side Metabase URL (1.0.13-T5 / C2).
"""

from pathlib import Path

from dango.security.metabase_config import resolve_metabase_url


def _write(root: Path, name: str, text: str) -> None:
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / name).write_text(text)


def _project_yml(root: Path, port: int) -> None:
    _write(
        root,
        "project.yml",
        f"project:\n  name: test\n  created_by: tester\n  purpose: test\nplatform:\n  metabase_port: {port}\n",
    )


def test_metadata_url_wins(tmp_path):
    _project_yml(tmp_path, 3555)
    _write(tmp_path, "metabase.yml", "metabase_url: http://localhost:3999/\n")
    assert resolve_metabase_url(tmp_path) == "http://localhost:3999"


def test_config_port_used_without_metadata(tmp_path):
    _project_yml(tmp_path, 3555)
    assert resolve_metabase_url(tmp_path) == "http://localhost:3555"


def test_default_when_nothing_configured(tmp_path):
    assert resolve_metabase_url(tmp_path) == "http://localhost:3000"


def test_malformed_metadata_falls_back(tmp_path):
    _project_yml(tmp_path, 3555)
    _write(tmp_path, "metabase.yml", "metabase_url: [unclosed\n")
    assert resolve_metabase_url(tmp_path) == "http://localhost:3555"
    (tmp_path / ".dango" / "project.yml").unlink()
    assert resolve_metabase_url(tmp_path) == "http://localhost:3000"


def test_empty_metadata_url_falls_back(tmp_path):
    _project_yml(tmp_path, 3555)
    for body in ("metabase_url: ''\n", "metabase_url: 42\n", "metabase_url:\n"):
        _write(tmp_path, "metabase.yml", body)
        assert resolve_metabase_url(tmp_path) == "http://localhost:3555"
