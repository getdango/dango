"""tests/unit/test_docker_cleanup_diagnostic.py

Tests for `dango docker-audit` (dango/cli/commands/docker_audit.py), the
machine-wide Docker resource diagnostic + cleanup command added in
1.0.8-Q8. This is the tool that should have existed before the real
2026-09-09 incident where an orphaned-looking scratch project's Docker
Compose identity was confused with a different, real project's identity
during manual cleanup, destroying that project's Metabase data.

1.0.8-Q12 extended this command to also read back
`com.dango.project_name`/`com.dango.project_id` labels (stamped onto the
metabase-data volume by `dango init`) so an orphaned volume's owning
project stays identifiable even after its container is removed — see
`TestGetVolumeLabels` and `TestBuildGroupsWithLabels`.

These are unit tests with mocked `subprocess.run`. Live verification
against a real Docker daemon (a real orphaned scratch project created by
`rm -rf`-ing its directory without stopping it first, and a real
currently-running scratch project that must never be flagged as cleanable)
was performed manually — see the PR description for the exact commands and
output.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands.docker_audit import (
    ResourceGroup,
    _build_groups,
    _get_volume_labels,
    _project_from_resource_name,
    _remove_group,
)


def _docker_ps_line(container_id: str, name: str, project: str, working_dir: str) -> str:
    return f"{container_id}\t{name}\t{project}\t{working_dir}"


def _volume_inspect_json(
    labels_by_name: dict[str, dict[str, str] | None],
) -> MagicMock:
    """Build a mocked `docker volume inspect ... --format json` response —
    a single JSON array of records, live-verified shape (Docker 28.5.1 /
    Compose v2.40.2): `Labels` is JSON `null`, not `{}`, on an unlabeled
    volume."""
    records = [{"Name": name, "Labels": labels} for name, labels in labels_by_name.items()]
    return MagicMock(returncode=0, stdout=json.dumps(records))


@pytest.mark.unit
class TestProjectFromResourceName:
    def test_volume_name_pattern(self):
        assert _project_from_resource_name("dango-1fc60021_metabase-data") == "dango-1fc60021"

    def test_image_name_pattern(self):
        assert _project_from_resource_name("dango-1fc60021-metabase") == "dango-1fc60021"

    def test_dbt_docs_image_suffix(self):
        assert _project_from_resource_name("dango-1fc60021-dbt-docs") == "dango-1fc60021"

    def test_no_match_returns_none(self):
        assert _project_from_resource_name("unrelated-thing") is None

    def test_non_dango_prefixed_volume_returns_none(self):
        assert _project_from_resource_name("other-project_data") is None


@pytest.mark.unit
class TestBuildGroupsClassification:
    """`_build_groups()` issues three subprocess.run calls in order:
    containers (`docker ps -a`), volumes (`docker volume ls`), images
    (`docker images`)."""

    def test_orphaned_when_working_dir_missing(self, tmp_path):
        """A container's working_dir no longer exists on disk -> orphaned,
        safe to clean."""
        missing_dir = str(tmp_path / "deleted-project")  # never created
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=_docker_ps_line(
                        "abc123", "dango-aaa1111-metabase-1", "dango-aaa1111", missing_dir
                    ),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        assert groups[0].classification == "orphaned"
        assert groups[0].project == "dango-aaa1111"

    def test_live_when_working_dir_exists(self, tmp_path):
        """A container's working_dir exists on disk -> live, never offered
        for cleanup."""
        live_dir = tmp_path / "live-project"
        live_dir.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=_docker_ps_line(
                        "def456", "dango-bbb2222-metabase-1", "dango-bbb2222", str(live_dir)
                    ),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        assert groups[0].classification == "live"

    def test_needs_attention_when_no_container(self):
        """A bare volume with no matching container -> needs_attention
        (cannot verify anything about it), never auto-cleaned."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # no containers
                MagicMock(returncode=0, stdout="dango-ccc3333_metabase-data\n"),
                _volume_inspect_json({"dango-ccc3333_metabase-data": None}),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        assert groups[0].classification == "needs_attention"
        assert groups[0].project == "dango-ccc3333"
        assert groups[0].volumes == ["dango-ccc3333_metabase-data"]

    def test_needs_attention_when_multiple_working_dirs(self, tmp_path):
        """Containers under the SAME project name reporting more than one
        distinct working_dir -- the literal signature of an identity
        collision -- must be flagged needs_attention, not orphaned or live,
        regardless of whether either directory exists on disk."""
        dir_a = str(tmp_path / "a")
        dir_b = str(tmp_path / "b")
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=(
                        _docker_ps_line("c1", "dango-ddd4444-metabase-1", "dango-ddd4444", dir_a)
                        + "\n"
                        + _docker_ps_line("c2", "dango-ddd4444-fake", "dango-ddd4444", dir_b)
                    ),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        assert groups[0].classification == "needs_attention"
        assert set(groups[0].working_dirs) == {dir_a, dir_b}

    def test_volumes_and_images_cross_referenced_to_container_group(self, tmp_path):
        """A volume and image sharing a project name with a live container
        are folded into that same group, not treated as separate entries."""
        live_dir = tmp_path / "live-project"
        live_dir.mkdir()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=_docker_ps_line(
                        "abc", "dango-eee5555-metabase-1", "dango-eee5555", str(live_dir)
                    ),
                ),
                MagicMock(returncode=0, stdout="dango-eee5555_metabase-data\n"),
                _volume_inspect_json({"dango-eee5555_metabase-data": None}),
                MagicMock(returncode=0, stdout="dango-eee5555-metabase\n"),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        g = groups[0]
        assert g.classification == "live"
        assert g.volumes == ["dango-eee5555_metabase-data"]
        assert g.images == ["dango-eee5555-metabase"]

    def test_fails_open_to_empty_on_docker_error(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            groups = _build_groups()
        assert groups == []


@pytest.mark.unit
class TestGetVolumeLabels:
    """`_get_volume_labels()` (1.0.8-Q12) — batch-fetches the
    com.dango.project_name/com.dango.project_id labels dango init (Q12+)
    stamps onto the metabase-data volume, so docker-audit can identify an
    orphaned volume's origin even after its container is gone."""

    def test_empty_names_returns_empty_dict_without_calling_docker(self):
        with patch("subprocess.run") as mock_run:
            result = _get_volume_labels([])
        assert result == {}
        mock_run.assert_not_called()

    def test_labels_present_populates_fields(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _volume_inspect_json(
                {
                    "dango-1fc60021_metabase-data": {
                        "com.dango.project_name": "acme-analytics",
                        "com.dango.project_id": "1fc60021deadbeef",
                    }
                }
            )
            result = _get_volume_labels(["dango-1fc60021_metabase-data"])
        assert result == {"dango-1fc60021_metabase-data": ("acme-analytics", "1fc60021deadbeef")}

    def test_labels_absent_returns_none_none_not_error(self):
        """A volume created before this fix shipped has no com.dango.*
        labels at all (Labels is JSON null) -> (None, None), not an error."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _volume_inspect_json({"dango-preexisting_metabase-data": None})
            result = _get_volume_labels(["dango-preexisting_metabase-data"])
        assert result == {"dango-preexisting_metabase-data": (None, None)}

    def test_labels_present_but_missing_dango_keys_returns_none_none(self):
        """A volume with some OTHER labels (not ours) also degrades to
        (None, None) for our two keys, not a KeyError."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _volume_inspect_json(
                {"dango-other_metabase-data": {"some.other.label": "x"}}
            )
            result = _get_volume_labels(["dango-other_metabase-data"])
        assert result == {"dango-other_metabase-data": (None, None)}

    def test_docker_error_fails_open_to_empty_dict(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _get_volume_labels(["dango-x_metabase-data"])
        assert result == {}

    def test_docker_timeout_fails_open_to_empty_dict(self):
        import subprocess as subprocess_module

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess_module.TimeoutExpired(cmd="docker", timeout=10)
            result = _get_volume_labels(["dango-x_metabase-data"])
        assert result == {}

    def test_malformed_json_fails_open_to_empty_dict(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json{{{")
            result = _get_volume_labels(["dango-x_metabase-data"])
        assert result == {}

    def test_batches_all_names_into_one_call(self):
        """N+1 subprocess calls would be slow with many volumes on the
        machine — confirm every name is passed to a single `docker volume
        inspect` invocation, not one call per volume."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _volume_inspect_json(
                {"vol-a": None, "vol-b": None, "vol-c": None}
            )
            _get_volume_labels(["vol-a", "vol-b", "vol-c"])
        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd == ["docker", "volume", "inspect", "vol-a", "vol-b", "vol-c", "--format", "json"]


@pytest.mark.unit
class TestBuildGroupsWithLabels:
    """`_build_groups()` populates project_name/project_id from a labeled
    volume, without changing classification — the label is purely
    additive display information (1.0.8-Q12)."""

    def test_labeled_volume_populates_group_fields_classification_unchanged(self):
        """A group with no container (needs_attention) still shows the
        real project name/id from its labeled volume — this is the actual
        bug being fixed: identity survives container removal."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # no containers
                MagicMock(returncode=0, stdout="dango-1fc60021_metabase-data\n"),
                _volume_inspect_json(
                    {
                        "dango-1fc60021_metabase-data": {
                            "com.dango.project_name": "acme-analytics",
                            "com.dango.project_id": "1fc60021deadbeef",
                        }
                    }
                ),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        g = groups[0]
        # The label must not leak into or change classification — a
        # container-less group is still needs_attention, never promoted
        # to cleanable just because we now know its name.
        assert g.classification == "needs_attention"
        assert g.project_name == "acme-analytics"
        assert g.project_id == "1fc60021deadbeef"

    def test_unlabeled_volume_leaves_fields_none(self):
        """A volume created before this fix shipped has no labels ->
        project_name/project_id stay None, not an error, and docker-audit
        keeps working exactly as it did before this fix."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout="dango-preexisting_metabase-data\n"),
                _volume_inspect_json({"dango-preexisting_metabase-data": None}),
                MagicMock(returncode=0, stdout=""),
            ]
            groups = _build_groups()

        assert len(groups) == 1
        assert groups[0].project_name is None
        assert groups[0].project_id is None
        assert groups[0].classification == "needs_attention"


@pytest.mark.unit
class TestRemoveGroupOnlyActsOnPassedGroup:
    """_remove_group() has no classification awareness of its own — the
    caller (docker_audit()) is responsible for only ever passing orphaned
    groups. These tests confirm it only issues commands for the resources
    listed on the group it's given, never anything else."""

    @patch("dango.cli.commands.docker_audit.console")
    def test_removes_containers_volumes_and_images_in_group(self, _mock_console):
        group = ResourceGroup(
            project="dango-fff6666",
            working_dirs=["/deleted/path"],
            containers=["dango-fff6666-metabase-1"],
            volumes=["dango-fff6666_metabase-data"],
            images=["dango-fff6666-metabase"],
            classification="orphaned",
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _remove_group(group)

        commands_run = [call.args[0] for call in mock_run.call_args_list]
        assert ["docker", "rm", "-f", "dango-fff6666-metabase-1"] in commands_run
        assert ["docker", "volume", "rm", "dango-fff6666_metabase-data"] in commands_run
        assert ["docker", "rmi", "dango-fff6666-metabase"] in commands_run

    @patch("dango.cli.commands.docker_audit.console")
    def test_empty_group_issues_no_commands(self, _mock_console):
        group = ResourceGroup(project="dango-000000", classification="orphaned")
        with patch("subprocess.run") as mock_run:
            _remove_group(group)
        mock_run.assert_not_called()


@pytest.mark.unit
class TestDockerAuditCLIOnlyOffersOrphanedGroup:
    """Integration-style test of the docker_audit() command itself: given a
    mix of live / needs_attention / orphaned groups, only the orphaned one
    may ever be removed, and only behind confirmation."""

    def test_cleanup_skips_when_declined(self, tmp_path):
        from click.testing import CliRunner

        from dango.cli.commands.docker_audit import docker_audit

        live_dir = tmp_path / "live"
        live_dir.mkdir()
        missing_dir = str(tmp_path / "gone")

        with (
            patch("subprocess.run") as mock_run,
            patch("dango.cli.commands.docker_audit._remove_group") as mock_remove,
        ):
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=(
                        _docker_ps_line("c1", "dango-live-1", "dango-1111", str(live_dir))
                        + "\n"
                        + _docker_ps_line("c2", "dango-orphan-1", "dango-2222", missing_dir)
                    ),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            runner = CliRunner()
            result = runner.invoke(docker_audit, input="no\n")

        assert result.exit_code == 0
        mock_remove.assert_not_called()

    def test_cleanup_with_yes_only_removes_orphaned_group(self, tmp_path):
        from click.testing import CliRunner

        from dango.cli.commands.docker_audit import docker_audit

        live_dir = tmp_path / "live"
        live_dir.mkdir()
        missing_dir = str(tmp_path / "gone")

        with (
            patch("subprocess.run") as mock_run,
            patch("dango.cli.commands.docker_audit._remove_group") as mock_remove,
        ):
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=(
                        _docker_ps_line("c1", "dango-live-1", "dango-1111", str(live_dir))
                        + "\n"
                        + _docker_ps_line("c2", "dango-orphan-1", "dango-2222", missing_dir)
                    ),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            runner = CliRunner()
            result = runner.invoke(docker_audit, ["--yes"])

        assert result.exit_code == 0
        mock_remove.assert_called_once()
        removed_group = mock_remove.call_args[0][0]
        assert removed_group.project == "dango-2222"
        assert removed_group.classification == "orphaned"

    def test_dry_run_never_removes_anything(self, tmp_path):
        from click.testing import CliRunner

        from dango.cli.commands.docker_audit import docker_audit

        missing_dir = str(tmp_path / "gone")

        with (
            patch("subprocess.run") as mock_run,
            patch("dango.cli.commands.docker_audit._remove_group") as mock_remove,
        ):
            mock_run.side_effect = [
                MagicMock(
                    returncode=0,
                    stdout=_docker_ps_line("c2", "dango-orphan-1", "dango-2222", missing_dir),
                ),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
            ]
            runner = CliRunner()
            result = runner.invoke(docker_audit, ["--dry-run"])

        assert result.exit_code == 0
        mock_remove.assert_not_called()
