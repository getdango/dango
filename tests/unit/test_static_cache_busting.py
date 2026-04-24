"""tests/unit/test_static_cache_busting.py

Unit tests for static asset cache busting (BUG-066).
"""

import hashlib

import pytest


@pytest.mark.unit
class TestStaticCacheBusting:
    """Verify MD5 content-based static URL generation."""

    def test_static_url_returns_versioned_path(self) -> None:
        """static_url() appends ?v=<8-hex-chars> for known files."""
        from dango.web.routes.ui import _static_url

        url = _static_url("css/main.css")
        assert url.startswith("/static/css/main.css?v=")
        hash_part = url.split("?v=")[1]
        assert len(hash_part) == 8
        assert all(c in "0123456789abcdef" for c in hash_part)

    def test_static_url_unknown_file_no_version(self) -> None:
        """static_url() returns bare /static/ path for unknown files."""
        from dango.web.routes.ui import _static_url

        url = _static_url("js/does-not-exist.js")
        assert url == "/static/js/does-not-exist.js"
        assert "?v=" not in url

    def test_different_files_have_different_hashes(self) -> None:
        """Different static files with different content produce different hashes."""
        from dango.web.routes.ui import _static_hashes

        # css/main.css and js/app.js have vastly different content
        assert _static_hashes["css/main.css"] != _static_hashes["js/app.js"]

    def test_static_url_uses_hash_from_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """static_url() returns the hash stored in _static_hashes, not a recomputed one."""
        import dango.web.routes.ui as ui_module

        monkeypatch.setitem(ui_module._static_hashes, "js/test.js", "deadbeef")
        url = ui_module._static_url("js/test.js")
        assert url == "/static/js/test.js?v=deadbeef"

    def test_hash_matches_actual_file_content(self) -> None:
        """Stored hashes match re-computing MD5 directly from each file."""
        from dango.web.routes.ui import _static_dir, _static_hashes

        for relative_path, stored_hash in _static_hashes.items():
            expected = hashlib.md5((_static_dir / relative_path).read_bytes()).hexdigest()[:8]
            assert stored_hash == expected, f"Hash mismatch for {relative_path}"

    def test_all_served_static_files_are_hashed(self) -> None:
        """All CSS and JS files referenced in templates are in the hash map."""
        from dango.web.routes.ui import _static_hashes

        expected = [
            "css/tailwind.min.css",
            "css/main.css",
            "css/catalog.css",
            "js/app.js",
            "js/logs.js",
            "js/lineage.js",
            "js/catalog.js",
        ]
        for path in expected:
            assert path in _static_hashes, f"Missing hash for {path}"
