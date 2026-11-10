"""tests/unit/test_g4f2_metabase_access.py

The per-user Metabase access state: recorded bridged sessions and the pending marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.auth import metabase_access as access


@pytest.mark.unit
class TestPendingMarker:
    def test_mark_and_clear(self, tmp_path: Path) -> None:
        assert access.is_pending(tmp_path, "user-1") is False
        access.mark_pending(tmp_path, "user-1")
        assert access.is_pending(tmp_path, "user-1") is True
        assert access.is_pending(tmp_path, "user-2") is False
        access.clear_pending(tmp_path, "user-1")
        assert access.is_pending(tmp_path, "user-1") is False

    def test_clearing_an_absent_marker_is_fine(self, tmp_path: Path) -> None:
        access.clear_pending(tmp_path, "user-1")

    def test_unsafe_user_id_is_not_pending_and_cannot_be_marked(self, tmp_path: Path) -> None:
        assert access.is_pending(tmp_path, "../escape") is False
        with pytest.raises(ValueError):
            access.mark_pending(tmp_path, "../escape")


@pytest.mark.unit
class TestRecordedSessions:
    def test_record_list_and_forget(self, tmp_path: Path) -> None:
        access.record_session(tmp_path, "u", "sess-a")
        access.record_session(tmp_path, "u", "sess-b")
        assert access.recorded_sessions(tmp_path, "u") == ["sess-a", "sess-b"]
        access.forget_sessions(tmp_path, "u", ["sess-a"])
        assert access.recorded_sessions(tmp_path, "u") == ["sess-b"]
        access.forget_sessions(tmp_path, "u", ["sess-b"])
        assert access.recorded_sessions(tmp_path, "u") == []

    def test_only_the_newest_sessions_are_kept(self, tmp_path: Path) -> None:
        for n in range(30):
            access.record_session(tmp_path, "u", f"sess-{n}")
        kept = access.recorded_sessions(tmp_path, "u")
        assert len(kept) == 20
        assert kept[-1] == "sess-29"

    def test_unsafe_session_id_is_not_recorded(self, tmp_path: Path) -> None:
        access.record_session(tmp_path, "u", "bad\nid")
        assert access.recorded_sessions(tmp_path, "u") == []

    def test_state_files_are_not_world_readable(self, tmp_path: Path) -> None:
        access.record_session(tmp_path, "u", "sess-a")
        path = tmp_path / ".dango" / "state" / "metabase_access" / "u.sessions"
        assert path.stat().st_mode & 0o077 == 0
