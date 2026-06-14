"""tests/unit/test_project_root_lifecycle.py

Tests for create_app() / get_project_root() / lifespan project_root lifecycle.
"""

import pytest
from fastapi import FastAPI

from dango.web.app import create_app


class TestCreateAppProjectRoot:
    """create_app() project_root handling."""

    def test_no_args_does_not_set_project_root(self):
        """create_app() with no args should NOT set project_root on app state."""
        result = create_app()
        assert not hasattr(result.state, "project_root")

    def test_with_project_root_sets_it(self, tmp_path):
        """create_app(project_root=path) should set it on app state."""
        result = create_app(project_root=tmp_path)
        assert result.state.project_root == tmp_path


class TestGetProjectRoot:
    """get_project_root() behavior."""

    def test_raises_when_unset(self, monkeypatch):
        """get_project_root() raises RuntimeError when project_root is not set."""
        fresh_app = FastAPI()
        # Patch the module-level `app` that get_project_root() lazily imports
        monkeypatch.setattr("dango.web.app.app", fresh_app)

        from dango.web.helpers import get_project_root

        with pytest.raises(RuntimeError, match="project_root not configured"):
            get_project_root()

    def test_returns_when_set(self, tmp_path, monkeypatch):
        """get_project_root() returns project_root when set on app state."""
        fresh_app = FastAPI()
        fresh_app.state.project_root = tmp_path
        monkeypatch.setattr("dango.web.app.app", fresh_app)

        from dango.web.helpers import get_project_root

        assert get_project_root() == tmp_path
