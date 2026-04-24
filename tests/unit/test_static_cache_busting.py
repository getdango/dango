"""tests/unit/test_static_cache_busting.py

Unit tests for static asset cache busting (BUG-066).
"""

import hashlib
from pathlib import Path

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

    def test_different_content_different_hash(self, tmp_path: Path) -> None:
        """Different file content produces different MD5 hashes."""
        file_a = tmp_path / "a.js"
        file_b = tmp_path / "b.js"
        file_a.write_text("console.log('hello');")
        file_b.write_text("console.log('world');")

        hash_a = hashlib.md5(file_a.read_bytes()).hexdigest()[:8]
        hash_b = hashlib.md5(file_b.read_bytes()).hexdigest()[:8]

        assert hash_a != hash_b

    def test_same_content_same_hash(self, tmp_path: Path) -> None:
        """Identical content always produces the same MD5 hash."""
        content = "console.log('identical');"
        file_a = tmp_path / "a.js"
        file_b = tmp_path / "b.js"
        file_a.write_text(content)
        file_b.write_text(content)

        hash_a = hashlib.md5(file_a.read_bytes()).hexdigest()[:8]
        hash_b = hashlib.md5(file_b.read_bytes()).hexdigest()[:8]

        assert hash_a == hash_b

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
