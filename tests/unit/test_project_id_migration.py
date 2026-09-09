"""tests/unit/test_project_id_migration.py

Tests for the persisted project.id + lazy migration added in 1.0.8-Q9.

Background: get_compose_project_name() used to derive a project's Docker
Compose identity from an MD5 hash of its path string — not a stable
identifier (see 1.0.8-Q8's test_docker_identity_guard.py for the incident
writeup). 1.0.8-Q9 replaces this with a persisted `project.id` (UUID) stored
in project.yml, read directly instead of hashed. A pre-upgrade project
without `project.id` is migrated lazily, exactly once, only inside
DockerManager.start_services()/stop_services(), driven by real Docker state
(reusing 1.0.8-Q8's _get_existing_container_working_dirs()).
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from dango.config.loader import ConfigLoader
from dango.config.models import DangoConfig, ProjectContext, SourcesConfig
from dango.platform.docker import DockerManager, _legacy_path_hash, get_compose_project_name

_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


@pytest.mark.unit
class TestNewProjectGetsIdAtInit:
    def test_new_project_gets_id_at_init(self, tmp_path):
        """The dango init save path (ConfigLoader.save_config) writes a real
        project.id into project.yml — not just a Pydantic default that's
        never persisted."""
        loader = ConfigLoader(tmp_path)
        config = DangoConfig(
            project=ProjectContext(
                name="Test Project",
                created_by="test@example.com",
                purpose="Unit testing",
            ),
            sources=SourcesConfig(),
        )
        loader.save_config(config)

        raw = loader.load_yaml(loader.project_file)
        persisted_id = raw.get("project", {}).get("id")
        assert persisted_id is not None
        assert _UUID_HEX_RE.match(persisted_id)


@pytest.mark.unit
class TestMigrationAdoptsLegacyHash:
    def test_existing_project_without_id_adopts_legacy_hash_if_containers_exist(
        self, tmp_project_dir
    ):
        """A pre-upgrade project.yml (no `id`) whose legacy-hash compose name
        already has containers adopts that exact legacy hash as project.id —
        zero disruption. This is the highest-blast-radius branch in the
        whole task: getting it wrong orphans every existing running Dango
        project on upgrade."""
        manager = DockerManager(tmp_project_dir)
        legacy_hash = _legacy_path_hash(tmp_project_dir)

        with patch(
            "dango.platform.docker._get_existing_container_working_dirs",
            return_value={str(tmp_project_dir.resolve())},
        ) as mock_check:
            resolved_id = manager._resolve_or_migrate_project_id()

        mock_check.assert_called_once_with(f"dango-{legacy_hash}")
        assert resolved_id == legacy_hash

        loader = ConfigLoader(tmp_project_dir)
        raw = loader.load_yaml(loader.project_file)
        assert raw["project"]["id"] == legacy_hash

        # The resulting compose project name is unchanged from what it was
        # pre-migration (the whole point: zero disruption).
        assert get_compose_project_name(tmp_project_dir) == f"dango-{legacy_hash}"


@pytest.mark.unit
class TestMigrationGeneratesFreshId:
    def test_existing_project_without_id_generates_fresh_id_if_no_containers_exist(
        self, tmp_project_dir
    ):
        """No containers under the legacy-hash name -> a fresh UUID is
        generated and persisted, not the legacy hash."""
        manager = DockerManager(tmp_project_dir)
        legacy_hash = _legacy_path_hash(tmp_project_dir)

        with patch(
            "dango.platform.docker._get_existing_container_working_dirs",
            return_value=set(),
        ) as mock_check:
            resolved_id = manager._resolve_or_migrate_project_id()

        mock_check.assert_called_once_with(f"dango-{legacy_hash}")
        assert resolved_id != legacy_hash
        assert _UUID_HEX_RE.match(resolved_id)

        loader = ConfigLoader(tmp_project_dir)
        raw = loader.load_yaml(loader.project_file)
        assert raw["project"]["id"] == resolved_id


@pytest.mark.unit
class TestMigrationRunsOnlyOnce:
    def test_migration_only_runs_once(self, tmp_project_dir):
        """After the first start/stop resolves and persists an id, a second
        call must not regenerate or change it, and must not re-invoke the
        legacy-hash/containers-exist check at all."""
        manager = DockerManager(tmp_project_dir)

        with patch(
            "dango.platform.docker._get_existing_container_working_dirs",
            return_value=set(),
        ) as mock_check:
            first_id = manager._resolve_or_migrate_project_id()
            assert mock_check.call_count == 1

            second_id = manager._resolve_or_migrate_project_id()
            # No new call to the containers-exist check on the second run.
            assert mock_check.call_count == 1

        assert second_id == first_id

        # A fresh manager instance (new process/session) reading the same
        # project.yml also sees the same, unchanged id.
        with patch("dango.platform.docker._get_existing_container_working_dirs") as mock_check_2:
            third_id = DockerManager(tmp_project_dir)._resolve_or_migrate_project_id()
        mock_check_2.assert_not_called()
        assert third_id == first_id

    def test_persisted_id_never_overwritten_by_start_or_stop(self, tmp_project_dir):
        """stop_services() calls the migration method internally — calling
        it twice in a row (as `dango stop` run twice would) must not change
        an already-persisted id.

        Note: `_assert_no_identity_collision()` (1.0.8-Q8) also calls
        `_get_existing_container_working_dirs()` on every start/stop call,
        independent of migration — this test only asserts on the persisted
        *value*, not call counts, since the migration-specific
        "doesn't re-check" guarantee is already covered precisely by
        `test_migration_only_runs_once` above (which calls the migration
        method directly, in isolation from the unrelated collision guard).
        """
        (tmp_project_dir / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_project_dir)

        with (
            patch(
                "dango.platform.docker._get_existing_container_working_dirs",
                return_value=set(),
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            manager.stop_services()

        loader = ConfigLoader(tmp_project_dir)
        first_id = loader.load_yaml(loader.project_file)["project"]["id"]

        with (
            patch(
                "dango.platform.docker._get_existing_container_working_dirs",
                return_value=set(),
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = ""
            mock_run.return_value.stderr = ""
            manager.stop_services()

        second_id = loader.load_yaml(loader.project_file)["project"]["id"]
        assert second_id == first_id


@pytest.mark.unit
class TestMovedProjectDirectoryKeepsIdentity:
    def test_moved_project_directory_keeps_same_compose_project_name(self, tmp_path_factory):
        """The core new capability: a persisted project.id travels with
        project.yml, so a project directory moved to a new path reconnects
        to the same compose project name."""
        original_root = tmp_path_factory.mktemp("original-location")
        (original_root / ".dango").mkdir()

        loader = ConfigLoader(original_root)
        config = DangoConfig(
            project=ProjectContext(
                name="Movable Project",
                created_by="test@example.com",
                purpose="Unit testing",
            ),
            sources=SourcesConfig(),
        )
        loader.save_config(config)
        original_name = get_compose_project_name(original_root)

        # Simulate `mv original_root moved_root` by copying project.yml's
        # exact bytes to a differently-named directory — the persisted id
        # is what's supposed to travel, not the path.
        moved_root = tmp_path_factory.mktemp("moved-location")
        (moved_root / ".dango").mkdir()
        (moved_root / ".dango" / "project.yml").write_bytes(
            (original_root / ".dango" / "project.yml").read_bytes()
        )

        moved_name = get_compose_project_name(moved_root)

        assert moved_name == original_name
        # Sanity: the two paths really are different, so this isn't trivially
        # true — the old path-hash scheme would have produced different names.
        assert str(original_root) != str(moved_root)
        assert _legacy_path_hash(original_root) != _legacy_path_hash(moved_root)


@pytest.mark.unit
class TestGetComposeProjectNameFallback:
    def test_falls_back_to_legacy_hash_when_no_project_yml(self, tmp_path):
        """No project.yml at all (e.g. a bare directory) -> deterministic
        legacy hash, not an error and not a random value."""
        assert get_compose_project_name(tmp_path) == f"dango-{_legacy_path_hash(tmp_path)}"

    def test_falls_back_to_legacy_hash_when_id_field_missing(self, tmp_project_dir):
        """project.yml exists but has no `id` field (pre-upgrade, not yet
        migrated) -> deterministic legacy hash, consistent across repeated
        calls (never a fresh random value)."""
        expected = f"dango-{_legacy_path_hash(tmp_project_dir)}"
        assert get_compose_project_name(tmp_project_dir) == expected
        assert get_compose_project_name(tmp_project_dir) == expected

    def test_reads_persisted_id_once_present(self, tmp_project_dir):
        """Once project.id is written to project.yml, get_compose_project_name
        reads it directly instead of falling back to the hash."""
        loader = ConfigLoader(tmp_project_dir)
        raw = loader.load_yaml(loader.project_file)
        raw["project"]["id"] = "abcdef0123456789abcdef0123456789"
        loader.save_yaml(raw, loader.project_file)

        assert get_compose_project_name(tmp_project_dir) == "dango-abcdef01"
