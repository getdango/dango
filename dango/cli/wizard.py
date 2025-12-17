"""
Project Initialization Wizard

Interactive wizard for creating new Dango projects.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import inquirer
from rich.console import Console
from rich.panel import Panel

from dango.config import (
    ProjectContext,
    SourcesConfig,
    DangoConfig,
    Stakeholder,
)

console = Console()


class ProjectWizard:
    """Interactive wizard for project initialization"""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.config: Optional[DangoConfig] = None

    def run(self) -> DangoConfig:
        """
        Run the wizard and return configuration.

        Returns:
            DangoConfig with user's choices
        """
        console.print()
        console.print(Panel(
            "[bold]Welcome to Dango![/bold]\n\n"
            "Let's set up your data project. This will take ~2 minutes.\n\n"
            "You can always change these settings later in .dango/project.yml",
            title="🍡 Project Setup",
            border_style="cyan"
        ))
        console.print()

        # Basic project info (simplified for MVP)
        project_name = self._ask_project_name()
        organization = self._ask_organization()

        # Try to get creator from git config, otherwise use default
        # (User can edit .dango/project.yml later if needed)
        created_by = self._get_git_user() or "Unknown"

        # Use simple defaults for everything else
        from dango import __version__
        project = ProjectContext(
            name=project_name,
            organization=organization,
            dango_version=__version__,
            created=datetime.now(),
            created_by=created_by,
            purpose="Data analytics project",
            getting_started=self._default_getting_started(),
        )

        sources = SourcesConfig()

        self.config = DangoConfig(
            project=project,
            sources=sources
        )

        # Summary
        self._print_summary()

        return self.config

    def _ask_project_name(self) -> str:
        """Ask for project name"""
        default_name = self.project_dir.name.replace('-', ' ').replace('_', ' ').title()

        while True:
            questions = [
                inquirer.Text(
                    'name',
                    message="Project name",
                    default=default_name
                )
            ]
            answers = inquirer.prompt(questions)
            if answers is None:
                raise KeyboardInterrupt()
            name = answers['name']

            # Validate: dbt requires names to start with letter/underscore, not digit
            sanitized = name.lower().replace(' ', '_').replace('-', '_')
            if sanitized and sanitized[0].isdigit():
                console.print("[yellow]Project name cannot start with a number (dbt requirement).[/yellow]")
                console.print("[yellow]Please enter a name starting with a letter.[/yellow]")
                default_name = name  # Keep their input as new default
                continue

            return name

    def _ask_organization(self) -> Optional[str]:
        """Ask for organization name (optional)"""
        console.print("[cyan]Optional - used in UI/Metabase[/cyan]")
        questions = [
            inquirer.Text(
                'organization',
                message="Organization name",
                default=""
            )
        ]
        answers = inquirer.prompt(questions)
        if answers is None:
            raise KeyboardInterrupt()
        org = answers.get('organization', '').strip()
        return org if org else None

    def _get_git_user(self) -> Optional[str]:
        """Get user name and email from git config"""
        import subprocess

        try:
            name_result = subprocess.run(
                ["git", "config", "user.name"],
                capture_output=True,
                text=True,
                timeout=2
            )
            email_result = subprocess.run(
                ["git", "config", "user.email"],
                capture_output=True,
                text=True,
                timeout=2
            )

            if name_result.returncode == 0 and email_result.returncode == 0:
                name = name_result.stdout.strip()
                email = email_result.stdout.strip()
                if name and email:
                    return f"{name} <{email}>"
        except Exception:
            pass

        return None

    def _ask_created_by(self) -> str:
        """Ask for creator info"""
        questions = [
            inquirer.Text(
                'created_by',
                message="Your name and email (e.g., 'Jane Doe <jane@company.com>')"
            )
        ]
        answers = inquirer.prompt(questions)
        return answers['created_by']

    def _ask_purpose(self) -> str:
        """Ask for project purpose"""
        console.print()
        console.print("[cyan]What's this project for?[/cyan]")
        console.print("  Examples:")
        console.print("  • Track daily sales and customer behavior for exec reporting")
        console.print("  • Monitor marketing campaign performance")
        console.print("  • Analyze subscription churn and retention")
        console.print()

        questions = [
            inquirer.Editor(
                'purpose',
                message="Purpose (opens editor)",
                default="# Why does this project exist? What is it used for?\n"
            )
        ]
        answers = inquirer.prompt(questions)
        purpose = answers['purpose'].strip()

        # Remove the default comment if user didn't change it
        if purpose.startswith("# Why does this project exist"):
            lines = purpose.split('\n')
            purpose = '\n'.join(lines[1:]).strip()

        return purpose or "Data analytics project"

    def _ask_stakeholders(self) -> list[Stakeholder]:
        """Ask for stakeholders"""
        stakeholders = []

        console.print()
        console.print("[cyan]Add stakeholders (press Ctrl+C to finish)[/cyan]")

        while True:
            try:
                console.print()
                questions = [
                    inquirer.Text('name', message="Name"),
                    inquirer.Text('role', message="Role (e.g., 'CMO - Primary dashboard user')"),
                    inquirer.Text('contact', message="Contact (email or slack)"),
                ]
                answers = inquirer.prompt(questions)

                if answers is None:
                    break

                stakeholder = Stakeholder(
                    name=answers['name'],
                    role=answers['role'],
                    contact=answers['contact']
                )
                stakeholders.append(stakeholder)

                add_more = inquirer.confirm(
                    message="Add another stakeholder?",
                    default=False
                )

                if not add_more:
                    break

            except KeyboardInterrupt:
                console.print()
                break

        return stakeholders

    def _ask_sla(self) -> str:
        """Ask for SLA"""
        console.print()
        console.print("[cyan]Data freshness SLA[/cyan]")
        console.print("  Examples:")
        console.print("  • Daily by 9am SGT")
        console.print("  • Every Monday morning")
        console.print("  • Real-time (< 5 min)")
        console.print("  • Weekly on Sundays")
        console.print()

        questions = [
            inquirer.Text(
                'sla',
                message="SLA",
                default="Daily by 9am"
            )
        ]
        answers = inquirer.prompt(questions)
        return answers['sla']

    def _ask_limitations(self) -> str:
        """Ask for limitations"""
        console.print()
        console.print("[cyan]Known limitations or gotchas[/cyan]")
        console.print("  Examples:")
        console.print("  • Shopify data has 24h delay")
        console.print("  • Stripe doesn't include refunds")
        console.print("  • Google Sheets limited to 10k rows")
        console.print()

        questions = [
            inquirer.Editor(
                'limitations',
                message="Limitations (opens editor)",
                default="# Known limitations, caveats, or gotchas\n"
            )
        ]
        answers = inquirer.prompt(questions)
        limitations = answers['limitations'].strip()

        # Remove the default comment if user didn't change it
        if limitations.startswith("# Known limitations"):
            lines = limitations.split('\n')
            limitations = '\n'.join(lines[1:]).strip()

        return limitations or None

    def _default_getting_started(self) -> str:
        """Default getting started guide"""
        return (
            "1. Add data sources: dango source add\n"
            "2. Sync data: dango sync\n"
            "3. Start platform: dango start\n"
            "4. Open dashboards: http://localhost:8800"
        )

    def _print_summary(self):
        """Print configuration summary - only show fields with meaningful values"""
        console.print()

        # Build summary with only populated fields
        summary_lines = []

        # Always show project name
        summary_lines.append(f"[bold]Project:[/bold] {self.config.project.name}")

        # Show organization if provided
        if self.config.project.organization:
            summary_lines.append(f"[bold]Organization:[/bold] {self.config.project.organization}")

        # Show created_by if not "Unknown"
        if self.config.project.created_by and self.config.project.created_by != "Unknown":
            summary_lines.append(f"[bold]Created by:[/bold] {self.config.project.created_by}")

        # Show purpose if meaningful
        if self.config.project.purpose and self.config.project.purpose != "Data analytics project":
            purpose_text = self.config.project.purpose[:100]
            if len(self.config.project.purpose) > 100:
                purpose_text += "..."
            summary_lines.append(f"[bold]Purpose:[/bold] {purpose_text}")

        # Show stakeholders if any
        if self.config.project.stakeholders:
            summary_lines.append(f"[bold]Stakeholders:[/bold] {len(self.config.project.stakeholders)}")

        # Show SLA if set
        if self.config.project.sla:
            summary_lines.append(f"[bold]SLA:[/bold] {self.config.project.sla}")

        console.print(Panel(
            "\n".join(summary_lines),
            title="📋 Configuration Summary",
            border_style="green"
        ))
        console.print()
