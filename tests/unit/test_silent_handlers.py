"""tests/unit/test_silent_handlers.py

Failures that used to be swallowed (droplet cleanup, pre-push hook, crash recording, Metabase link) are surfaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.init import ProjectInitializer
from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.provisioning import provision_droplet

_CLEANUP_LOGGER = "dango.platform.cloud.droplet_cleanup._logger"


def _assert_names_orphan(exc: BaseException, droplet_id: int) -> None:
    text = str(exc) + "".join(getattr(exc, "__notes__", []))
    assert str(droplet_id) in text
    assert f"doctl compute droplet delete {droplet_id}" in text
    assert "billed" in text


@pytest.mark.unit
class TestOrphanedDropletCleanup:
    def test_provisioning_cleanup_failure_names_orphaned_droplet(self) -> None:
        client = MagicMock()
        client.create_droplet.return_value = {"id": 42, "status": "new"}
        client.delete_droplet.side_effect = RuntimeError("api down")

        with (
            patch("dango.platform.cloud.provisioning.wait_for_droplet_ready") as poll,
            patch(_CLEANUP_LOGGER) as log,
        ):
            poll.side_effect = CloudError("timeout", error_code="DANGO-D011")
            with pytest.raises(CloudError) as info:
                provision_droplet(client, "n", "nyc1", "s-2vcpu-4gb", ssh_key_ids=[])

        assert info.value.error_code == "DANGO-D011"
        _assert_names_orphan(info.value, 42)
        assert info.value.user_message.startswith("timeout")
        assert log.error.call_args.args[0] == "orphaned_droplet"
        assert log.error.call_args.kwargs["droplet_id"] == 42

    def test_migrate_cleanup_failure_names_orphaned_droplet(self, tmp_path: Path) -> None:
        from dango.platform.cloud.migrate import migrate_server
        from tests.unit.test_migrate import (
            _make_cloud_config,
            _make_do_client,
            _make_ssh_mock,
            _setup_migrate_patches,
        )

        client = _make_do_client()
        client.delete_droplet.side_effect = RuntimeError("api down")
        patches = _setup_migrate_patches(
            tmp_path, setup_error=CloudProvisioningError("Setup failed")
        )
        with (
            patches["backup"],
            patches["upload"],
            patches["provision"],
            patches["ssh_cls"],
            patches["setup"],
            patch(_CLEANUP_LOGGER) as log,
            pytest.raises(CloudProvisioningError, match="Setup failed") as info,
        ):
            migrate_server(
                client,
                _make_ssh_mock(),
                _make_cloud_config(),
                "s-4vcpu-8gb",
                "sfo3",
                project_root=tmp_path,
            )

        _assert_names_orphan(info.value, 99)
        assert log.error.call_args.kwargs["droplet_id"] == 99

    def test_annotate_without_add_note_rebuilds_message(self) -> None:
        """The Python 3.10 path: no add_note, plain exception with one string arg."""
        from dango.platform.cloud.droplet_cleanup import _annotate

        class NoNotes(Exception):
            def __getattribute__(self, name: str) -> Any:
                if name == "add_note":
                    raise AttributeError(name)
                return super().__getattribute__(name)

        exc = NoNotes("boom")
        _annotate(exc, "extra")
        assert str(exc) == "boom\nextra"

    def test_successful_delete_leaves_error_untouched(self) -> None:
        from dango.platform.cloud.droplet_cleanup import delete_droplet_or_annotate

        exc = CloudError("x")
        delete_droplet_or_annotate(MagicMock(), 1, exc)
        assert str(exc) == "x"


@pytest.mark.unit
class TestInitHookWarning:
    def test_init_warns_when_hook_fails_but_succeeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        initializer = ProjectInitializer(tmp_path / "proj")
        with (
            patch.object(ProjectInitializer, "_setup_metabase", return_value=True),
            patch.object(ProjectInitializer, "_generate_dbt_docs", return_value=True),
            patch.object(ProjectInitializer, "_setup_auth", return_value=True),
            patch.object(
                ProjectInitializer, "_create_pre_push_hook", side_effect=OSError("denied")
            ),
            patch("dango.cli.init.console") as console,
        ):
            initializer.initialize(skip_wizard=True)  # must not raise

        printed = " ".join(str(c.args[0]) for c in console.print.call_args_list if c.args)
        assert "pre-push" in printed
        assert "denied" in printed
        assert str(Path(".git") / "hooks" / "pre-push") in printed


@pytest.mark.unit
class TestCrashRecordingLogged:
    @pytest.mark.parametrize(
        ("target", "step"),
        [
            ("dango.utils.sync_history.save_sync_history_entry", "history"),
            ("dango.utils.activity_log.log_activity", "activity_log"),
            ("dango.utils.dbt_status.mark_source_models_stale", "stale_models"),
        ],
    )
    def test_sync_crash_recording_failures_are_logged(
        self, tmp_path: Path, target: str, step: str
    ) -> None:
        from dango.platform.sync_process import _handle_crash

        with (
            patch("dango.utils.sync_history.save_sync_history_entry"),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.utils.dbt_status.mark_source_models_stale"),
            patch(target, side_effect=RuntimeError("disk full")),
            patch("dango.platform.sync_process.logger") as log,
        ):
            _handle_crash(tmp_path, ["src"], 1, None, "crashed")

        steps = [
            c.kwargs.get("step")
            for c in log.warning.call_args_list
            if c.args and c.args[0] == "crash_recording_step_failed"
        ]
        assert steps == [step]


@pytest.mark.unit
class TestMetabaseBestEffortLogged:
    def test_discard_pending_failure_is_logged(self) -> None:
        from dango.platform.common import metabase_admin_repair as repair

        store: Any = MagicMock()
        store.discard_pending.side_effect = OSError("locked")
        with patch.object(repair, "_logger", create=True) as log:
            repair._discard_pending(store)
        assert log.warning.call_args.args[0] == "pending_credential_discard_failed"

    def test_link_failure_is_logged_without_secrets(self, tmp_path: Path) -> None:
        from dango.platform.common.metabase_link import link_metabase_admin

        with (
            patch(
                "dango.security.metabase_config.load_metabase_metadata",
                side_effect=RuntimeError("secret-pw"),
            ),
            patch("dango.logging.get_logger") as get_logger,
        ):
            link_metabase_admin(tmp_path, "a@b.c")

        call = get_logger.return_value.warning.call_args
        assert call.args[0] == "metabase_admin_link_failed"
        assert "secret-pw" not in str(call)
