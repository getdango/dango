"""tests/unit/test_config_loader.py

Tests for dango.config.loader — ConfigLoader and module-level functions.
"""

import pytest
import yaml

from dango.config.exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from dango.config.helpers import (
    check_unreferenced_custom_sources,
    format_unreferenced_sources_warning,
    get_config,
    load_config,
    save_config,
)
from dango.config.loader import ConfigLoader
from dango.config.models import (
    DangoConfig,
    DataSource,
    DltNativeConfig,
    SourcesConfig,
    SourceType,
)
from tests.factories.config_factories import (
    make_dango_config,
    make_project_context,
    make_sources_config,
)


@pytest.mark.unit
class TestConfigLoaderPaths:
    def test_sets_paths_from_project_root(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        assert loader.project_root == tmp_path
        assert loader.dango_dir == tmp_path / ".dango"
        assert loader.project_file == tmp_path / ".dango" / "project.yml"
        assert loader.sources_file == tmp_path / ".dango" / "sources.yml"

    def test_defaults_to_cwd(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        loader = ConfigLoader()
        assert loader.project_root == tmp_path


@pytest.mark.unit
class TestIsDangoProject:
    def test_true_when_dango_dir_and_project_file_exist(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        assert loader.is_dango_project() is True

    def test_false_when_dango_dir_missing(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        assert loader.is_dango_project() is False

    def test_false_when_project_file_missing(self, tmp_path):
        (tmp_path / ".dango").mkdir()
        loader = ConfigLoader(project_root=tmp_path)
        assert loader.is_dango_project() is False


@pytest.mark.unit
class TestFindProjectRoot:
    def test_found_in_current_dir(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        result = loader.find_project_root(start_path=tmp_project_dir)
        assert result == tmp_project_dir

    def test_found_in_parent(self, tmp_project_dir):
        child = tmp_project_dir / "subdir"
        child.mkdir()
        loader = ConfigLoader(project_root=tmp_project_dir)
        result = loader.find_project_root(start_path=child)
        assert result == tmp_project_dir

    def test_not_found_returns_none(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        result = loader.find_project_root(start_path=tmp_path)
        assert result is None


@pytest.mark.unit
class TestLoadYaml:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "test.yml"
        f.write_text("key: value\n")
        loader = ConfigLoader(project_root=tmp_path)
        result = loader.load_yaml(f)
        assert result == {"key": "value"}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        f = tmp_path / "empty.yml"
        f.write_text("")
        loader = ConfigLoader(project_root=tmp_path)
        result = loader.load_yaml(f)
        assert result == {}

    def test_missing_file_raises(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigNotFoundError):
            loader.load_yaml(tmp_path / "nonexistent.yml")

    def test_invalid_yaml_raises(self, tmp_path):
        f = tmp_path / "bad.yml"
        f.write_text("key: [unclosed bracket\n")
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigError):
            loader.load_yaml(f)


@pytest.mark.unit
class TestSaveYaml:
    def test_creates_file(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        out = tmp_path / "out.yml"
        loader.save_yaml({"key": "value"}, out)
        assert out.exists()
        content = yaml.safe_load(out.read_text())
        assert content == {"key": "value"}

    def test_creates_parent_dirs(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        out = tmp_path / "a" / "b" / "out.yml"
        loader.save_yaml({"nested": True}, out)
        assert out.exists()

    def test_overwrites_existing(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        out = tmp_path / "out.yml"
        loader.save_yaml({"old": True}, out)
        loader.save_yaml({"new": True}, out)
        content = yaml.safe_load(out.read_text())
        assert content == {"new": True}

    def test_no_leftover_tmp_file(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        out = tmp_path / "out.yml"
        loader.save_yaml({"key": "value"}, out)
        tmp_file = out.with_suffix(".yml.tmp")
        assert not tmp_file.exists()


@pytest.mark.unit
class TestLoadProjectContext:
    def test_valid(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        ctx = loader.load_project_context()
        assert ctx.name == "Test Project"
        assert ctx.created_by == "test@example.com"

    def test_missing_file_raises(self, tmp_path):
        (tmp_path / ".dango").mkdir()
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigNotFoundError):
            loader.load_project_context()

    def test_invalid_data_raises(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        # project key exists but missing required fields
        (dango_dir / "project.yml").write_text(yaml.safe_dump({"project": {"name": "P"}}))
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigValidationError):
            loader.load_project_context()


@pytest.mark.unit
class TestLoadSourcesConfig:
    def test_valid(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        sc = loader.load_sources_config()
        assert len(sc.sources) == 1
        assert sc.sources[0].name == "test_csv"

    def test_missing_returns_empty(self, tmp_path):
        (tmp_path / ".dango").mkdir()
        loader = ConfigLoader(project_root=tmp_path)
        sc = loader.load_sources_config()
        assert sc.sources == []

    def test_invalid_data_raises(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text(
            yaml.safe_dump({"sources": [{"name": "bad-name", "type": "csv"}]})
        )
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigValidationError):
            loader.load_sources_config()


@pytest.mark.unit
class TestLoadConfig:
    def test_valid_complete(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        config = loader.load_config()
        assert isinstance(config, DangoConfig)
        assert config.project.name == "Test Project"

    def test_with_platform_settings(self, tmp_project_dir):
        # Add platform settings to project.yml
        project_file = tmp_project_dir / ".dango" / "project.yml"
        data = yaml.safe_load(project_file.read_text())
        data["platform"] = {"port": 9999}
        project_file.write_text(yaml.safe_dump(data))

        loader = ConfigLoader(project_root=tmp_project_dir)
        config = loader.load_config()
        assert config.platform.port == 9999

    def test_missing_project_file_raises(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        with pytest.raises(ConfigNotFoundError):
            loader.load_config()


@pytest.mark.unit
class TestSaveProjectContext:
    def test_creates_file(self, tmp_path):
        (tmp_path / ".dango").mkdir()
        loader = ConfigLoader(project_root=tmp_path)
        ctx = make_project_context()
        loader.save_project_context(ctx)
        assert loader.project_file.exists()

    def test_preserves_existing_platform(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        # Write project.yml with platform settings
        initial = {
            "project": {"name": "P", "created_by": "a@b.com", "purpose": "test"},
            "platform": {"port": 9999},
        }
        (dango_dir / "project.yml").write_text(yaml.safe_dump(initial))

        loader = ConfigLoader(project_root=tmp_path)
        ctx = make_project_context(name="Updated")
        loader.save_project_context(ctx)

        data = yaml.safe_load(loader.project_file.read_text())
        assert data["project"]["name"] == "Updated"
        assert data["platform"]["port"] == 9999


@pytest.mark.unit
class TestSaveSourcesConfig:
    def test_round_trip(self, tmp_path):
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        loader = ConfigLoader(project_root=tmp_path)
        original = make_sources_config()
        loader.save_sources_config(original)

        loaded = loader.load_sources_config()
        assert len(loaded.sources) == len(original.sources)
        assert loaded.sources[0].name == original.sources[0].name


@pytest.mark.unit
class TestSaveConfig:
    def test_writes_both_files(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        config = make_dango_config()
        loader.save_config(config)
        assert loader.project_file.exists()
        assert loader.sources_file.exists()


@pytest.mark.unit
class TestValidateConfig:
    def test_valid_returns_true(self, tmp_project_dir):
        loader = ConfigLoader(project_root=tmp_project_dir)
        is_valid, errors = loader.validate_config()
        assert is_valid is True
        assert errors == []

    def test_invalid_returns_false(self, tmp_path):
        loader = ConfigLoader(project_root=tmp_path)
        is_valid, errors = loader.validate_config()
        assert is_valid is False
        assert len(errors) > 0


@pytest.mark.unit
class TestModuleLevelFunctions:
    def test_get_config(self, tmp_project_dir):
        config = get_config(project_root=tmp_project_dir)
        assert isinstance(config, DangoConfig)

    def test_load_config_alias(self, tmp_project_dir):
        config = load_config(project_root=tmp_project_dir)
        assert isinstance(config, DangoConfig)

    def test_save_config(self, tmp_path):
        config = make_dango_config()
        save_config(config, project_root=tmp_path)
        assert (tmp_path / ".dango" / "project.yml").exists()


@pytest.mark.unit
class TestCheckUnreferencedCustomSources:
    def test_no_custom_sources_dir(self, tmp_path):
        sc = make_sources_config(sources=[])
        result = check_unreferenced_custom_sources(tmp_path, sc)
        assert result == []

    def test_all_referenced(self, tmp_path):
        cs_dir = tmp_path / "custom_sources"
        cs_dir.mkdir()
        (cs_dir / "my_source.py").write_text("# source")

        ds = DataSource(
            name="my_native",
            type=SourceType.DLT_NATIVE,
            dlt_native=DltNativeConfig(
                source_module="my_source",
                source_function="my_func",
            ),
        )
        sc = SourcesConfig(sources=[ds])
        result = check_unreferenced_custom_sources(tmp_path, sc)
        assert result == []

    def test_unreferenced_detected(self, tmp_path):
        cs_dir = tmp_path / "custom_sources"
        cs_dir.mkdir()
        (cs_dir / "orphan.py").write_text("# source")

        sc = make_sources_config(sources=[])
        result = check_unreferenced_custom_sources(tmp_path, sc)
        assert "orphan" in result

    def test_ignores_init_and_dotfiles(self, tmp_path):
        cs_dir = tmp_path / "custom_sources"
        cs_dir.mkdir()
        (cs_dir / "__init__.py").write_text("")
        (cs_dir / ".hidden.py").write_text("")

        sc = make_sources_config(sources=[])
        result = check_unreferenced_custom_sources(tmp_path, sc)
        assert result == []


@pytest.mark.unit
class TestFormatUnreferencedSourcesWarning:
    def test_empty_list_returns_empty_string(self):
        assert format_unreferenced_sources_warning([]) == ""

    def test_single_unreferenced(self):
        result = format_unreferenced_sources_warning(["my_source"])
        assert "my_source" in result
        assert "custom_sources/my_source.py" in result

    def test_multiple_unreferenced(self):
        result = format_unreferenced_sources_warning(["src_a", "src_b"])
        assert "src_a" in result
        assert "src_b" in result
