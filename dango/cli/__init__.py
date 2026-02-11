"""dango/cli/__init__.py

Dango CLI module.

Provides command-line interface for Dango data platform.
"""

from rich.console import Console

# Shared console instance for all CLI command modules.
# Enable hyperlinks in terminal output (for clickable URLs).
console = Console(force_terminal=True, legacy_windows=False)
