"""dango/config/cloud_credentials.py

Persistent storage for cloud provider credentials.

Stores credentials in ``~/.dango/credentials`` (INI format, ``0o600``
permissions).  Environment variables always take precedence over stored
values.

BUG-127: Without persistence, every ``dango deploy``/``destroy``/``remote``
command re-prompts for the DigitalOcean API token.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

_CREDENTIALS_DIR = Path.home() / ".dango"
_CREDENTIALS_FILE = _CREDENTIALS_DIR / "credentials"
_SECTION = "digitalocean"
_TOKEN_KEY = "api_token"


def _read_config() -> configparser.ConfigParser:
    """Read the credentials file, returning an empty config if missing."""
    config = configparser.ConfigParser()
    if _CREDENTIALS_FILE.is_file():
        config.read(str(_CREDENTIALS_FILE))
    return config


def _write_config(config: configparser.ConfigParser) -> None:
    """Write *config* to the credentials file with secure permissions."""
    _CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    # Use os.open() for atomic secure file creation (0o600)
    fd = os.open(
        str(_CREDENTIALS_FILE),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w") as f:
            config.write(f)
    except Exception:
        os.close(fd)
        raise


def get_do_token() -> str | None:
    """Return the DigitalOcean API token.

    Resolution order:
    1. ``DIGITALOCEAN_TOKEN`` environment variable
    2. Stored credential in ``~/.dango/credentials``
    """
    env_token = os.environ.get("DIGITALOCEAN_TOKEN")
    if env_token:
        return env_token

    config = _read_config()
    if config.has_option(_SECTION, _TOKEN_KEY):
        return config.get(_SECTION, _TOKEN_KEY) or None

    return None


def save_do_token(token: str) -> None:
    """Persist *token* to ``~/.dango/credentials``."""
    config = _read_config()
    if not config.has_section(_SECTION):
        config.add_section(_SECTION)
    config.set(_SECTION, _TOKEN_KEY, token)
    _write_config(config)


def clear_do_token() -> bool:
    """Remove the stored DigitalOcean token.

    Returns:
        ``True`` if a token was removed, ``False`` if none was stored.
    """
    config = _read_config()
    if not config.has_option(_SECTION, _TOKEN_KEY):
        return False
    config.remove_option(_SECTION, _TOKEN_KEY)
    # Remove empty section
    if not config.options(_SECTION):
        config.remove_section(_SECTION)
    _write_config(config)
    return True
