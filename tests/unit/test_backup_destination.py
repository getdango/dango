"""tests/unit/test_backup_destination.py

Off-site backup destination read by the server (AF-10), truthful off-site status, the secret NAMES in a backup manifest, and a local restore that leaves this machine's cloud.yml alone.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from tests.factories.backup_create import (
    archive_text,
    run_scheduled_archive,
    run_server_archive,
)

_SB = "dango.platform.cloud.scheduled_backup"
_BD = "dango.platform.cloud.backup_destination"

_ENV_KEYS = "SPACES_ACCESS_KEY=ak\nSPACES_SECRET_KEY=sk\n"


def _project(tmp_path: Path, env: str | None = None, cloud_yml: str | None = None) -> Path:
    project = tmp_path / "srv-project"
    (project / ".dango").mkdir(parents=True)
    if env is not None:
        (project / ".env").write_text(env)
    if cloud_yml is not None:
        (project / ".dango" / "cloud.yml").write_text(cloud_yml)
    return project


# ---------------------------------------------------------------------------
# AF-10: the server reads the destination deploy wrote to its .env
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDestinationFromServerEnv:
    def test_bucket_and_region_come_from_the_server_env(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(
            tmp_path, env=_ENV_KEYS + "SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"
        )
        with patch(f"{_SB}.PROJECT_DIR", project):
            assert _load_spaces_config() == {"bucket": "my-bucket", "region": "sgp1"}

    def test_env_wins_over_cloud_yml(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(
            tmp_path,
            env="SPACES_BUCKET=env-bucket\nSPACES_REGION=sgp1\n",
            cloud_yml="spaces:\n  bucket: yml-bucket\n  region: nyc3\n",
        )
        with patch(f"{_SB}.PROJECT_DIR", project):
            assert _load_spaces_config()["bucket"] == "env-bucket"

    def test_cloud_yml_is_still_the_fallback(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(
            tmp_path, env="FOO=1\n", cloud_yml="spaces:\n  bucket: yml-bucket\n  region: nyc3\n"
        )
        with patch(f"{_SB}.PROJECT_DIR", project):
            assert _load_spaces_config() == {"bucket": "yml-bucket", "region": "nyc3"}

    @pytest.mark.parametrize(
        "bucket", ["bad bucket", "../etc", "UPPER", "a;rm -rf /", "x" * 80, "$(id)", "-lead"]
    )
    def test_invalid_bucket_in_env_is_refused(self, tmp_path: Path, bucket: str) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(tmp_path, env=f"SPACES_BUCKET={bucket}\nSPACES_REGION=sgp1\n")
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError, match="SPACES_BUCKET"):
                _load_spaces_config()

    @pytest.mark.parametrize("region", ["sgp 1", "SGP1", "../x", "a" * 40, "sgp1;id"])
    def test_invalid_region_in_env_is_refused(self, tmp_path: Path, region: str) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(tmp_path, env=f"SPACES_BUCKET=ok-bucket\nSPACES_REGION={region}\n")
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError, match="SPACES_REGION"):
                _load_spaces_config()

    def test_invalid_bucket_in_cloud_yml_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(tmp_path, cloud_yml="spaces:\n  bucket: 'bad bucket;id'\n")
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError):
                _load_spaces_config()

    def test_an_env_symlink_is_not_followed(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _load_spaces_config

        project = _project(tmp_path)
        target = tmp_path / "elsewhere.env"
        target.write_text("SPACES_BUCKET=other-bucket\nSPACES_REGION=sgp1\n")
        (project / ".env").symlink_to(target)
        with patch(f"{_SB}.PROJECT_DIR", project):
            with pytest.raises(CloudProvisioningError):
                _load_spaces_config()


class _FakeSpaces:
    """Records what a scheduled backup does against Spaces."""

    instances: list[_FakeSpaces] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.bucket = str(kwargs["bucket"])
        self.uploaded: dict[str, bytes] = {}
        _FakeSpaces.instances.append(self)

    def upload(self, key: str, fileobj: io.BufferedReader, content_type: str = "") -> None:
        self.uploaded[key] = fileobj.read()

    def list_objects(self, prefix: str = "") -> list[dict[str, object]]:
        return []

    def _get_client(self) -> MagicMock:
        client = MagicMock()
        client.head_object.side_effect = lambda **kw: {
            "ContentLength": max((len(v) for v in self.uploaded.values()), default=0)
        }
        return client


def _scheduled_run(tmp_path: Path, project: Path) -> object:
    from dango.platform.cloud.scheduled_backup import run_scheduled_backup

    backups = tmp_path / "bk"
    backups.mkdir()
    archive = backups / "backup-20261101-020000.tar.gz"
    archive.write_bytes(b"archive-bytes")
    _FakeSpaces.instances = []
    with (
        patch(f"{_SB}.PROJECT_DIR", project),
        patch(f"{_SB}.BACKUP_DIR", backups),
        patch(f"{_SB}.HEALTH_FILE", tmp_path / "health.json"),
        patch(f"{_SB}.MARKER_FILE", tmp_path / "marker"),
        patch(f"{_SB}._acquire_backup_lock", return_value=MagicMock()),
        patch(f"{_SB}.fcntl.flock"),
        patch(f"{_SB}._create_local_archive", return_value=(archive, {}, [])),
        patch("dango.platform.cloud.spaces.SpacesClient", _FakeSpaces),
    ):
        return run_scheduled_backup()


@pytest.mark.unit
class TestDeployedServerUploads:
    def test_a_server_deployed_with_backups_uploads_to_spaces(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path, env=_ENV_KEYS + "SPACES_BUCKET=my-bucket\nSPACES_REGION=sgp1\n"
        )
        result = _scheduled_run(tmp_path, project)

        assert result.error is None
        assert result.spaces_key == "backups/backup-20261101-020000.tar.gz"
        assert not any("Spaces not configured" in w for w in result.warnings)
        client = _FakeSpaces.instances[0]
        assert client.kwargs["bucket"] == "my-bucket"
        assert client.kwargs["region"] == "sgp1"
        assert client.kwargs["access_key"] == "ak"
        assert client.uploaded == {"backups/backup-20261101-020000.tar.gz": b"archive-bytes"}
        assert result.off_site is True
        assert result.spaces_bucket == "my-bucket"

    def test_without_a_destination_the_run_says_it_is_local_only(self, tmp_path: Path) -> None:
        project = _project(tmp_path, env="FOO=1\n")
        result = _scheduled_run(tmp_path, project)

        assert result.error is None
        assert result.spaces_key == ""
        assert result.off_site is False
        assert _FakeSpaces.instances == []


@pytest.mark.unit
class TestOffsiteLine:
    def test_yes_names_the_bucket(self) -> None:
        from dango.platform.cloud.backup_destination import offsite_line

        assert offsite_line("my-bucket") == "Off-site: yes (Spaces my-bucket)"

    def test_no_says_backups_are_lost_with_the_server(self) -> None:
        from dango.platform.cloud.backup_destination import offsite_line

        line = offsite_line(None)
        assert line.startswith("Off-site: NO: ")
        assert "only on this server" in line
        assert "lost with it" in line

    def test_an_unverified_upload_is_not_reported_as_yes(self) -> None:
        from dango.platform.cloud.backup_destination import offsite_line

        assert not offsite_line("my-bucket", verified=False).startswith("Off-site: yes")

    def test_the_module_entry_point_prints_the_line(self, tmp_path: Path) -> None:
        import runpy

        from dango.platform.cloud.scheduled_backup import ScheduledBackupResult

        result = ScheduledBackupResult(archive_path="/x/a.tar.gz", off_site=True)
        result.spaces_bucket = "my-bucket"
        printed: list[str] = []
        with (
            patch(f"{_SB}.run_scheduled_backup", return_value=result),
            patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(a))),
            pytest.raises(SystemExit),
        ):
            runpy.run_module("dango.platform.cloud.scheduled_backup", run_name="__main__")
        assert "Off-site: yes (Spaces my-bucket)" in printed


# ---------------------------------------------------------------------------
# Secret NAMES (never values)
# ---------------------------------------------------------------------------

_PEM = "MIIEvgIBADANBgkqhkiG9w0BAQEFAASC\nAbCdEfGh1234567890==\n-----END KEY-----"


@pytest.mark.unit
class TestSecretNames:
    def test_env_names_only(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        text = "# c\nA_KEY=value-one\nexport B_KEY='value two'\n\nC=\n"
        assert env_secret_names(text) == ["A_KEY", "B_KEY", "C"]

    def test_a_multiline_quoted_value_never_yields_names(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        text = f'REAL_KEY="{_PEM}"\nNEXT=1\n'
        names = env_secret_names(text)
        assert names == ["NEXT", "REAL_KEY"] or names == ["REAL_KEY", "NEXT"]
        assert "AbCdEfGh1234567890" not in names

    def test_hostile_names_are_dropped(self) -> None:
        from dango.platform.cloud.backup_destination import env_secret_names

        text = "GOOD=1\n$(id)=2\nA B=3\n../x=4\nBAD;NAME=5\n" + "N" * 300 + "=6\n"
        assert env_secret_names(text) == ["GOOD"]

    def test_toml_key_paths_without_values(self) -> None:
        from dango.platform.cloud.backup_destination import toml_secret_names

        text = (
            '[sources.google_ads]\nclient_secret = "s3cr3t-value"\n'
            'refresh_token = """line1\nline2"""\n[destination]\npassword = "p"\n'
        )
        names = toml_secret_names(text)
        assert names == [
            "destination.password",
            "sources.google_ads.client_secret",
            "sources.google_ads.refresh_token",
        ]
        assert not any("s3cr3t" in n for n in names)

    def test_invalid_toml_gives_no_names(self) -> None:
        from dango.platform.cloud.backup_destination import toml_secret_names

        assert toml_secret_names("this is = = not toml [") == []

    def test_server_names_come_from_env_and_secrets_toml(self) -> None:
        from dango.platform.cloud.backup_destination import server_secret_names

        files = {
            "/srv/dango/project/.env": "API_KEY=abc\nDATABASE_URL=x\n",
            "/srv/dango/project/.dlt/secrets.toml": '[a]\nk = "v"\n',
        }
        with patch(f"{_BD}.read_project_file", side_effect=lambda _ssh, p: files[p]):
            assert server_secret_names(MagicMock()) == ["API_KEY", "DATABASE_URL", "a.k"]

    def test_unreadable_server_files_give_no_names(self) -> None:
        from dango.platform.cloud.backup_destination import server_secret_names

        with patch(f"{_BD}.read_project_file", side_effect=CloudProvisioningError("no")):
            assert server_secret_names(MagicMock()) == []


_HOSTILE_ENV = (
    "API_KEY=value-api-111\n"
    'PRIVATE="-----BEGIN-----\nAbCdEfGh1234567890==\nZZtop99==\n-----END-----"\n'
    "TOKEN=value-token-222\n"
)
_TOML = '[sources.s]\nclient_secret = "value-toml-333"\n'
_VALUES = ("value-api-111", "value-token-222", "value-toml-333", "AbCdEfGh1234567890", "ZZtop99")


def _secrets_project(root: Path) -> Path:
    project = root / "proj"
    (project / ".dango").mkdir(parents=True)
    (project / ".dlt").mkdir()
    (project / ".env").write_text(_HOSTILE_ENV)
    (project / ".dlt" / "secrets.toml").write_text(_TOML)
    return project


def _local_read(_ssh: object, path: str) -> str:
    return Path(path.replace("/srv/dango/project", str(_LOCAL_ROOT[0]))).read_text()


_LOCAL_ROOT: list[Path] = [Path(".")]


@pytest.mark.unit
class TestManifestListsNamesOnly:
    @pytest.fixture(params=["server", "scheduled"])
    def creator(self, request: pytest.FixtureRequest) -> str:
        return str(request.param)

    def _manifest(self, creator: str, tmp_path: Path, include_secrets: bool) -> tuple[dict, bytes]:
        project = _secrets_project(tmp_path)
        _LOCAL_ROOT[0] = project
        if creator == "server":
            with patch(f"{_BD}.read_project_file", side_effect=_local_read):
                archive, _m, _w, _ssh = run_server_archive(
                    tmp_path, project, include_secrets=include_secrets
                )
        else:
            archive, _m, _w = run_scheduled_archive(
                tmp_path, project, include_secrets=include_secrets, checkpoint=lambda: []
            )
        raw = archive_text(archive, "manifest.json")
        sidecar = archive.with_suffix("").with_suffix(".json")
        assert sidecar.read_bytes() == raw or json.loads(sidecar.read_text()) == json.loads(raw)
        return json.loads(raw), raw + sidecar.read_bytes()

    def test_manifest_lists_names(self, creator: str, tmp_path: Path) -> None:
        manifest, _raw = self._manifest(creator, tmp_path, include_secrets=False)
        assert set(manifest["secret_names"]) == {
            "API_KEY",
            "PRIVATE",
            "TOKEN",
            "sources.s.client_secret",
        }
        assert manifest["secrets_included"] is False

    def test_no_value_lands_in_the_manifest(self, creator: str, tmp_path: Path) -> None:
        _manifest, raw = self._manifest(creator, tmp_path, include_secrets=False)
        text = raw.decode()
        leaked = [v for v in _VALUES if v in text]
        assert not leaked, f"{creator} manifest holds secret values: {leaked}"

    def test_included_secrets_are_flagged(self, creator: str, tmp_path: Path) -> None:
        manifest, _raw = self._manifest(creator, tmp_path, include_secrets=True)
        assert manifest["secrets_included"] is True


# ---------------------------------------------------------------------------
# Reading names back from an archive
# ---------------------------------------------------------------------------


def _archive_bytes(manifest: dict | None, *, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        members = dict(extra or {})
        if manifest is not None:
            members["manifest.json"] = json.dumps(manifest).encode()
        for name, data in members.items():
            info = tarfile.TarInfo(f"backup-1/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.unit
class TestArchiveSecretsInfo:
    def test_reads_names_and_flag(self) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        data = _archive_bytes({"secret_names": ["A", "b.c"], "secrets_included": False})
        assert archive_secrets_info(data) == (["A", "b.c"], False)

    def test_accepts_a_path(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        path = tmp_path / "a.tar.gz"
        path.write_bytes(_archive_bytes({"secret_names": ["A"], "secrets_included": True}))
        assert archive_secrets_info(path) == (["A"], True)

    def test_an_older_manifest_has_no_names(self) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        assert archive_secrets_info(_archive_bytes({"files": []})) == (None, False)

    def test_no_manifest_or_not_an_archive(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        assert archive_secrets_info(_archive_bytes(None)) == (None, False)
        assert archive_secrets_info(b"not a tarball") == (None, False)

    def test_hostile_entries_are_dropped(self) -> None:
        from dango.platform.cloud.backup_destination import archive_secrets_info

        manifest = {"secret_names": ["GOOD", "bad name\nPASSWORD=hunter2", 5, None, "x" * 500]}
        assert archive_secrets_info(_archive_bytes(manifest)) == (["GOOD"], False)

    def test_reenter_message(self) -> None:
        from dango.platform.cloud.backup_destination import reenter_message

        text = reenter_message(["A", "B"])
        assert text.startswith("secrets are not in this backup; re-enter: A, B")
        assert "re-enter" not in reenter_message([]).split(";", 1)[1]
        assert "older" in reenter_message(None)


# ---------------------------------------------------------------------------
# cloud.yml: never part of a backup, never overwritten by a local restore
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCloudYmlStaysLocal:
    def test_cloud_yml_is_excluded_not_backed_up(self) -> None:
        from dango.platform.cloud.backup import BACKUP_FILES, EXCLUDED_PATHS

        assert ".dango/cloud.yml" not in BACKUP_FILES
        assert EXCLUDED_PATHS[".dango/cloud.yml"].strip()

    @pytest.mark.parametrize("creator", ["server", "scheduled"])
    def test_archives_do_not_hold_cloud_yml(self, creator: str, tmp_path: Path) -> None:
        project = _project(tmp_path, cloud_yml="droplet_id: 42\n")
        (project / ".dango" / "project.yml").write_text("a: 1\n")
        if creator == "server":
            _a, members, _w, _s = run_server_archive(tmp_path, project)
        else:
            _a, members, _w = run_scheduled_archive(tmp_path, project, checkpoint=lambda: [])
        assert ".dango/project.yml" in members
        assert ".dango/cloud.yml" not in members

    def test_local_restore_of_an_older_archive_keeps_local_cloud_yml(self, tmp_path: Path) -> None:
        from dango.cli.commands.local_backup import _extract_archive

        project = tmp_path / "local-project"
        (project / ".dango").mkdir(parents=True)
        (project / ".dango" / "cloud.yml").write_text("droplet_id: 111\nfirewall_id: fw-local\n")
        (project / ".dango" / "project.yml").write_text("old: true\n")
        archive = tmp_path / "old-backup.tar.gz"
        archive.write_bytes(
            _archive_bytes(
                None,
                extra={
                    ".dango/cloud.yml": b"droplet_id: 999\n",
                    ".dango/project.yml": b"restored: true\n",
                },
            )
        )

        skipped = _extract_archive(archive, project)

        assert skipped == []
        assert (project / ".dango" / "project.yml").read_text() == "restored: true\n"
        assert (project / ".dango" / "cloud.yml").read_text() == (
            "droplet_id: 111\nfirewall_id: fw-local\n"
        )

    def test_local_restore_creates_no_cloud_yml_when_there_was_none(self, tmp_path: Path) -> None:
        from dango.cli.commands.local_backup import _extract_archive

        project = tmp_path / "local-project"
        (project / ".dango").mkdir(parents=True)
        archive = tmp_path / "old-backup.tar.gz"
        archive.write_bytes(_archive_bytes(None, extra={".dango/cloud.yml": b"droplet_id: 999\n"}))

        _extract_archive(archive, project)

        assert not (project / ".dango" / "cloud.yml").exists()
