"""tests/unit/test_git_info.py

Unit tests for dango/utils/git_info.py — git info collection and guardrails.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.utils.git_info import (
    GitInfo,
    check_git_guardrails,
    collect_git_info,
)

# ---------------------------------------------------------------------------
# collect_git_info
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectGitInfo:
    """Test collect_git_info against real and mock git repos."""

    def test_real_git_repo(self, tmp_path: Path) -> None:
        """Create a real git repo and verify info is collected."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        # Create initial commit
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        info = collect_git_info(tmp_path)

        assert info.is_git_repo is True
        assert info.commit_sha is not None
        assert len(info.commit_sha) == 40
        assert info.is_clean is True

    def test_clean_vs_dirty(self, tmp_path: Path) -> None:
        """Dirty working tree should be detected."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

        # Clean state
        info = collect_git_info(tmp_path)
        assert info.is_clean is True

        # Make it dirty
        (tmp_path / "dirty.txt").write_text("uncommitted")
        info = collect_git_info(tmp_path)
        assert info.is_clean is False

    def test_non_git_directory(self, tmp_path: Path) -> None:
        """Non-git directory should return is_git_repo=False."""
        info = collect_git_info(tmp_path)
        assert info.is_git_repo is False
        assert info.commit_sha is None
        assert info.branch is None

    def test_git_not_installed(self, tmp_path: Path) -> None:
        """FileNotFoundError from subprocess should return gracefully."""
        with patch("dango.utils.git_info._run_git", return_value=None):
            info = collect_git_info(tmp_path)
            assert info.is_git_repo is False

    def test_branch_detection(self, tmp_path: Path) -> None:
        """Branch name should be detected."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        (tmp_path / "file.txt").write_text("hello")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature-branch"],
            check=True,
            capture_output=True,
        )

        info = collect_git_info(tmp_path)
        assert info.branch == "feature-branch"


# ---------------------------------------------------------------------------
# check_git_guardrails
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckGitGuardrails:
    """Test pre-deployment git guardrails."""

    def test_not_a_git_repo(self) -> None:
        """Non-git repo should pass with warning."""
        info = GitInfo(is_git_repo=False)
        result = check_git_guardrails(info)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "Not a git" in result.warnings[0]

    def test_correct_branch_clean(self) -> None:
        """Correct branch + clean tree should pass with no issues."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="main",
            is_clean=True,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main")
        assert result.passed is True
        assert result.errors == []
        assert result.warnings == []

    def test_wrong_branch_blocked(self) -> None:
        """Wrong branch should fail."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="develop",
            is_clean=True,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main")
        assert result.passed is False
        assert len(result.errors) == 1
        assert "develop" in result.errors[0]
        assert "main" in result.errors[0]

    def test_wrong_branch_with_allow(self) -> None:
        """Wrong branch with allow_branch should pass with warning."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="develop",
            is_clean=True,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main", allow_branch=True)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "develop" in result.warnings[0]

    def test_dirty_tree_blocked(self) -> None:
        """Dirty tree should fail."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="main",
            is_clean=False,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main")
        assert result.passed is False
        assert len(result.errors) == 1
        assert "uncommitted" in result.errors[0]

    def test_dirty_tree_with_allow(self) -> None:
        """Dirty tree with allow_dirty should pass with warning."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="main",
            is_clean=False,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main", allow_dirty=True)
        assert result.passed is True
        assert len(result.warnings) == 1
        assert "uncommitted" in result.warnings[0]

    def test_both_wrong_branch_and_dirty(self) -> None:
        """Both errors should be reported."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="develop",
            is_clean=False,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main")
        assert result.passed is False
        assert len(result.errors) == 2

    def test_detached_head_warning(self) -> None:
        """Detached HEAD should produce a warning."""
        info = GitInfo(
            commit_sha="a" * 40,
            branch="HEAD",
            is_clean=True,
            is_git_repo=True,
        )
        result = check_git_guardrails(info, expected_branch="main", allow_branch=True)
        assert result.passed is True
        assert any("Detached HEAD" in w for w in result.warnings)
