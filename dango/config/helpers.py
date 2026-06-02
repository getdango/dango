"""dango/config/helpers.py

Config convenience functions for loading, saving, and finding project configuration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .exceptions import ProjectNotFoundError
from .loader import ConfigLoader

if TYPE_CHECKING:
    from .models import CloudConfig, DangoConfig, SourcesConfig


def find_project_root(start_path: Path | None = None) -> Path:
    """
    Find Dango project root directory.

    Args:
        start_path: Starting directory (defaults to current directory)

    Returns:
        Project root path

    Raises:
        ProjectNotFoundError: If not in a Dango project
    """
    loader = ConfigLoader(start_path)
    root = loader.find_project_root(start_path)

    if root is None:
        raise ProjectNotFoundError(
            "Not in a Dango project.\nRun 'dango init' to create a new project."
        )

    return root


def get_config(project_root: Path | None = None) -> DangoConfig:
    """
    Helper function to load config.

    Args:
        project_root: Project root directory (defaults to current directory)

    Returns:
        DangoConfig instance
    """
    loader = ConfigLoader(project_root)
    return loader.load_config()


def load_config(project_root: Path | None = None) -> DangoConfig:
    """
    Alias for get_config - load configuration.

    Args:
        project_root: Project root directory (defaults to current directory)

    Returns:
        DangoConfig instance
    """
    return get_config(project_root)


def save_config(config: DangoConfig, project_root: Path | None = None) -> None:
    """
    Helper function to save config.

    Args:
        config: DangoConfig instance to save
        project_root: Project root directory (defaults to current directory)
    """
    loader = ConfigLoader(project_root)
    loader.save_config(config)


def is_cloud_mode(project_root: Path) -> bool:
    """Check if this project has an active cloud deployment.

    Returns True if the ``DANGO_CLOUD_MODE`` environment variable is set to
    ``"true"`` (used on the server where ``cloud.yml`` does not exist), or if
    ``.dango/cloud.yml`` exists locally and contains a ``droplet_ip``.

    .. note::

       This conflates two questions: "has this project been deployed?" and
       "am I running on the cloud server?". Prefer :func:`has_cloud_deployment`
       or :func:`is_running_on_cloud` when you need to distinguish.
    """
    return is_running_on_cloud() or has_cloud_deployment(project_root)


def has_cloud_deployment(project_root: Path) -> bool:
    """Check if this project has been deployed to a cloud server.

    Returns True if ``.dango/cloud.yml`` exists and contains a ``droplet_ip``.
    This does NOT mean the current process is running on the cloud — it may be
    running locally on a machine that has deployed to cloud.
    """
    loader = ConfigLoader(project_root)
    cloud_cfg: CloudConfig | None = loader.load_cloud_config()
    return cloud_cfg is not None and cloud_cfg.droplet_ip is not None


def is_running_on_cloud() -> bool:
    """Check if the current process is running on a cloud server.

    Returns True only if the ``DANGO_CLOUD_MODE`` environment variable is
    set to ``"true"``. This env var is set by the systemd unit on cloud
    servers.
    """
    import os

    return os.environ.get("DANGO_CLOUD_MODE") == "true"


def get_cloud_origin(project_root: Path) -> str | None:
    """Return the public origin URL for a cloud deployment.

    Returns ``https://{domain}`` if a domain is configured,
    ``http://{ip}`` if only an IP is available, or ``None`` if
    there is no cloud deployment.
    """
    loader = ConfigLoader(project_root)
    cloud_cfg: CloudConfig | None = loader.load_cloud_config()
    if cloud_cfg is None or cloud_cfg.droplet_ip is None:
        return None
    if cloud_cfg.domain:
        return f"https://{cloud_cfg.domain}"
    return f"http://{cloud_cfg.droplet_ip}"


def check_unreferenced_custom_sources(
    project_dir: Path, sources_config: SourcesConfig
) -> list[str]:
    """
    Find Python files in custom_sources/ that aren't referenced in sources.yml.

    This helps users who create custom dlt sources but forget to add
    the corresponding dlt_native entry to sources.yml.

    Args:
        project_dir: Project root directory
        sources_config: Loaded sources configuration

    Returns:
        List of unreferenced Python module names (without .py extension)
    """
    custom_sources_dir = project_dir / "custom_sources"
    if not custom_sources_dir.exists():
        return []

    # Get all .py files (excluding __init__.py and __pycache__)
    py_files = [
        f.stem
        for f in custom_sources_dir.glob("*.py")
        if f.name not in ("__init__.py",) and not f.name.startswith(".")
    ]

    # Get referenced modules from dlt_native sources
    referenced = set()
    for source in sources_config.sources:
        if source.type == "dlt_native" and source.dlt_native:
            referenced.add(source.dlt_native.source_module)

    # Return unreferenced modules
    return [f for f in py_files if f not in referenced]


def format_unreferenced_sources_warning(unreferenced: list[str]) -> str:
    """
    Format a helpful warning message for unreferenced custom sources.

    Args:
        unreferenced: List of unreferenced module names

    Returns:
        Formatted warning message with actionable instructions
    """
    if not unreferenced:
        return ""

    files_list = "\n".join(f"   - custom_sources/{f}.py" for f in unreferenced)
    example_name = unreferenced[0]

    return f"""
⚠️  Unreferenced custom sources detected:
{files_list}

These files won't be synced. To use them, add to .dango/sources.yml:

  - name: {example_name}
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: {example_name}
      source_function: <function_name>
      function_kwargs: {{}}

Docs: https://docs.getdango.dev/custom-sources
"""
