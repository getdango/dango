"""
.env File Helpers

Utilities for creating, validating, and managing .env files for credentials.
"""

import os
import platform
import subprocess
from pathlib import Path
from typing import List, Dict, Set, Optional, Tuple
from rich.console import Console

console = Console()


def create_env_template(
    env_file: Path,
    env_vars: List[Dict[str, str]],
    backup: bool = True
) -> None:
    """
    Create or update .env file with helpful templates for required variables.

    Uses atomic write pattern to prevent data loss on failure.

    Args:
        env_file: Path to .env file
        env_vars: List of env var configs with name, help, format, example
        backup: Whether to backup existing .env before modifying
    """
    env_file.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content (or start with header for new file)
    if env_file.exists():
        original_content = env_file.read_text()
        existing_lines = original_content.splitlines()
    else:
        original_content = ""
        existing_lines = [
            "# Dango Data Platform - Environment Variables",
            "# Add your credentials below",
            ""
        ]

    # Backup existing file if requested
    if backup and env_file.exists():
        backup_file = env_file.with_suffix('.env.backup')
        backup_file.write_text(original_content)

    # Parse existing vars
    existing_vars = set()
    for line in existing_lines:
        if '=' in line and not line.strip().startswith('#'):
            var_name = line.split('=')[0].strip()
            existing_vars.add(var_name)

    # Build new vars section (only if not already present)
    new_content = []
    for var_config in env_vars:
        var_name = var_config.get('name')

        if var_name not in existing_vars:
            # Add section header with source information
            source_name = var_config.get('source_name')
            source_type = var_config.get('source_type')

            if source_name and source_type:
                new_content.append(f"\n# {var_config.get('display_name', var_name)}")
                new_content.append(f"# For source: '{source_name}' ({source_type})")
            else:
                new_content.append(f"\n# {var_config.get('display_name', var_name)}")

            # Add help text
            if 'help' in var_config:
                new_content.append(f"# {var_config['help']}")

            # Add format hint
            if 'format' in var_config:
                new_content.append(f"# Format: {var_config['format']}")

            # Add example
            if 'example' in var_config:
                new_content.append(f"# Example: {var_config['example']}")

            # Add empty var line
            new_content.append(f"{var_name}=")
            new_content.append("")  # Blank line

    # Atomic write: temp file + rename
    if new_content or not env_file.exists():
        temp_file = env_file.with_suffix('.env.tmp')
        try:
            # Write combined content to temp file
            with open(temp_file, 'w') as f:
                if existing_lines:
                    f.write('\n'.join(existing_lines))
                if new_content:
                    f.write('\n'.join(new_content))

            # Atomic rename
            temp_file.replace(env_file)

        except Exception as e:
            # Clean up temp file and restore original if it existed
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass
            raise Exception(f"Failed to update .env file: {e}")


def validate_env_file(
    env_file: Path,
    required_vars: List[str]
) -> Tuple[bool, List[str]]:
    """
    Validate that .env file contains all required variables with non-empty values.

    Args:
        env_file: Path to .env file
        required_vars: List of required variable names

    Returns:
        Tuple of (all_valid, missing_vars)
    """
    if not env_file.exists():
        return False, required_vars

    # Parse .env file
    env_values = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            env_values[key] = value

    # Check required vars
    missing = []
    for var in required_vars:
        if var not in env_values or not env_values[var]:
            missing.append(var)

    return (len(missing) == 0, missing)


def open_file_in_default_app(filepath: Path) -> bool:
    """
    Open file in OS default application (non-blocking).

    Args:
        filepath: Path to file to open

    Returns:
        True if opened successfully, False otherwise
    """
    try:
        system = platform.system()

        if system == "Darwin":  # macOS
            subprocess.Popen(["open", str(filepath)])
        elif system == "Windows":
            subprocess.Popen(["start", str(filepath)], shell=True)
        elif system == "Linux":
            subprocess.Popen(["xdg-open", str(filepath)])
        else:
            return False

        return True
    except Exception:
        return False


def guide_env_setup(
    env_file: Path,
    required_vars: List[Dict[str, str]],
    source_name: str,
    setup_guide: List[str] = None
) -> bool:
    """
    Guide user through .env setup with optional file opening and validation.

    Args:
        env_file: Path to .env file
        required_vars: List of required var configs (with name, help, etc.)
        source_name: Name of source being configured
        setup_guide: Optional list of setup instructions to display

    Returns:
        True if validated successfully, False otherwise
    """
    from inquirer import prompt, Confirm
    from inquirer.themes import GreenPassion

    console.print(f"\n[bold cyan]📝 Credentials Setup[/bold cyan]")
    console.print(f"File: [dim]{env_file.absolute()}[/dim]\n")

    # Show detailed setup guide if provided
    if setup_guide:
        console.print("[bold]How to get your credentials:[/bold]")
        for step in setup_guide:
            console.print(f"  {step}")
        console.print()

    # Show required credentials
    console.print("[bold]Required credentials:[/bold]")
    for var_config in required_vars:
        var_name = var_config.get('name')
        help_text = var_config.get('help', '')
        console.print(f"  • {var_name}")
        if help_text:
            console.print(f"    [dim]{help_text}[/dim]")

    # Offer to open file
    console.print()
    questions = [
        Confirm(
            'open_file',
            message="Would you like me to open .env in your editor?",
            default=True
        )
    ]
    answers = prompt(questions, theme=GreenPassion())

    if answers and answers['open_file']:
        if open_file_in_default_app(env_file):
            console.print("[green]✅ Opened .env in your default editor[/green]")
            console.print("\n[bold cyan]Next steps:[/bold cyan]")
            console.print("  1. Find the section with empty values (e.g., STRIPE_API_KEY=)")
            console.print("  2. Paste your credential after the = sign")
            console.print("  3. [bold]SAVE THE FILE[/bold] (Cmd+S or Ctrl+S)")
            console.print("  4. Return here and press Enter")
        else:
            console.print("[yellow]⚠️  Couldn't open automatically[/yellow]")
            console.print(f"Please manually open: {env_file}")
            console.print("\n[bold cyan]Then:[/bold cyan]")
            console.print("  1. Find the section with empty values")
            console.print("  2. Paste your credentials after the = sign")
            console.print("  3. [bold]SAVE THE FILE[/bold]")

    # Wait for user to finish
    console.print("\n[bold yellow]⚠️  Press Enter AFTER you've saved the .env file[/bold yellow]")
    input()

    # Validate
    var_names = [v.get('name') for v in required_vars]
    is_valid, missing = validate_env_file(env_file, var_names)

    return handle_validation_result(
        is_valid, missing, env_file, source_name
    )


def handle_validation_result(
    is_valid: bool,
    missing_vars: List[str],
    env_file: Path,
    source_name: str
) -> bool:
    """
    Handle validation results with retry logic and graceful failure.

    Args:
        is_valid: Whether all required vars are present
        missing_vars: List of missing variable names
        env_file: Path to .env file
        source_name: Name of source being configured

    Returns:
        True if validation passed or user chose to skip
    """
    from inquirer import prompt, List as InquirerList
    from inquirer.themes import GreenPassion

    if is_valid:
        console.print("\n[green]✅ All credentials validated successfully![/green]")
        return True

    # Validation failed
    console.print("\n[yellow]⚠️  Credential Validation Failed[/yellow]")
    console.print("\nMissing or empty variables:")
    for var in missing_vars:
        console.print(f"  • {var}")

    console.print(f"\n[bold]Note:[/bold] Source '{source_name}' has been saved to sources.yml")
    console.print("However, it won't work until you add valid credentials.\n")

    # Offer retry
    questions = [
        InquirerList(
            'action',
            message="What would you like to do?",
            choices=[
                'Edit .env again and retry validation',
                'Skip validation (I\'ll add credentials later)',
                'Cancel source setup'
            ],
        )
    ]
    answers = prompt(questions, theme=GreenPassion())

    if not answers:
        return False

    action = answers['action']

    if action == 'Edit .env again and retry validation':
        # Reopen file
        if open_file_in_default_app(env_file):
            console.print("[green]✅ Reopened .env[/green]")

        console.print("\n[bold]Press Enter when ready to retry validation...[/bold]")
        input()

        # Retry validation
        is_valid_retry, missing_retry = validate_env_file(env_file, missing_vars)
        return handle_validation_result(
            is_valid_retry, missing_retry, env_file, source_name
        )

    elif action == 'Skip validation (I\'ll add credentials later)':
        console.print("\n[yellow]⚠️  Skipping validation[/yellow]")
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print(f"  1. Edit .env and add missing credentials")
        console.print(f"  2. Validate with: [bold]dango config validate[/bold]")
        console.print(f"  3. Or try syncing: [bold]dango sync --source {source_name}[/bold]\n")
        return True  # Allow wizard to complete

    else:  # Cancel
        console.print("\n[red]Source setup cancelled[/red]")
        console.print(f"The source config was saved but is incomplete.")
        console.print(f"To remove it, edit .dango/sources.yml\n")
        return False
