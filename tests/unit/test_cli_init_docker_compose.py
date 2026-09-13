"""tests/unit/test_cli_init_docker_compose.py

Tests for ProjectInitializer._create_docker_compose() (dango/cli/init.py),
specifically the 1.0.8-Q12 change that passes `project_id=config.project.id`
to the `docker-compose.yml.j2` template render so the rendered
`metabase-data` volume carries a `com.dango.project_id` label alongside
`com.dango.project_name` — see `dango/cli/commands/docker_audit.py` for the
reader side (`_get_volume_labels()`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.init import ProjectInitializer
from dango.config.models import DangoConfig, ProjectContext


def _make_config(project_name: str = "My Project") -> DangoConfig:
    return DangoConfig(
        project=ProjectContext(
            name=project_name,
            purpose="testing",
            created_by="test-user",
        )
    )


@pytest.mark.unit
class TestCreateDockerCompose:
    def test_renders_project_id_label(self, tmp_path: Path) -> None:
        """The rendered docker-compose.yml embeds the real project.id in
        the com.dango.project_id label, not a placeholder or empty string."""
        config = _make_config()
        initializer = ProjectInitializer(tmp_path)
        initializer._create_docker_compose(config)

        content = (tmp_path / "docker-compose.yml").read_text()
        assert f'com.dango.project_id: "{config.project.id}"' in content

    def test_renders_project_name_label(self, tmp_path: Path) -> None:
        """The com.dango.project_name label uses the same slugified name
        already used for MB_SITE_NAME etc. (lowercased, spaces -> dashes)."""
        config = _make_config(project_name="My Project")
        initializer = ProjectInitializer(tmp_path)
        initializer._create_docker_compose(config)

        content = (tmp_path / "docker-compose.yml").read_text()
        assert 'com.dango.project_name: "my-project"' in content

    def test_project_id_is_stable_across_reads(self, tmp_path: Path) -> None:
        """config.project.id is a persisted UUID hex (1.0.8-Q9), not
        regenerated per render — confirm the same config object renders
        the same id both label occurrences would use."""
        config = _make_config()
        initializer = ProjectInitializer(tmp_path)
        initializer._create_docker_compose(config)

        content = (tmp_path / "docker-compose.yml").read_text()
        assert content.count(config.project.id) == 1  # only in project_id label

    def test_both_services_have_bounded_log_rotation(self, tmp_path: Path) -> None:
        """1.0.8: metabase and dbt-docs both cap their own container stdout/stderr
        capture (json-file driver, max-size/max-file) — previously unbounded, a
        long-lived project (especially an unattended cloud deployment) could
        accumulate this indefinitely. This is Docker's own container-level log
        capture, unrelated to and never touching dango's application-level audit
        log (auth/audit.py -> .dango/logs/audit.jsonl), which already has its own
        separate gzip-rotation (utils/log_rotation.py)."""
        import yaml

        config = _make_config()
        initializer = ProjectInitializer(tmp_path)
        initializer._create_docker_compose(config)

        compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
        for service_name in ("metabase", "dbt-docs"):
            logging_config = compose["services"][service_name]["logging"]
            assert logging_config["driver"] == "json-file"
            assert logging_config["options"]["max-size"] == "10m"
            assert logging_config["options"]["max-file"] == "3"
