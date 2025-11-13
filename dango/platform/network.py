"""
Network & nginx Management

Manages shared nginx instance for clean URLs across multiple Dango projects.
Architecture: Shared nginx on port 80, routing by domain to different backend ports.

Created: MVP Week 1 Day 1 (Oct 27, 2025)
"""

import json
import socket
import subprocess
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from datetime import datetime
import os


class NetworkConfig:
    """
    Manages ~/.dango/ global network configuration

    Responsibilities:
    - Manage routing.json (registry of all projects)
    - Allocate backend ports
    - Track nginx state
    """

    def __init__(self):
        self.dango_home = Path.home() / ".dango"
        self.routing_file = self.dango_home / "routing.json"
        self.shared_nginx_dir = self.dango_home / "shared-nginx"
        self.sites_dir = self.shared_nginx_dir / "sites"
        self.logs_dir = self.shared_nginx_dir / "logs"

        # Ensure directories exist
        self.dango_home.mkdir(exist_ok=True)
        self.shared_nginx_dir.mkdir(exist_ok=True)
        self.sites_dir.mkdir(exist_ok=True)
        self.logs_dir.mkdir(exist_ok=True)

    def load_routing(self) -> Dict:
        """Load routing.json or create default"""
        if not self.routing_file.exists():
            return {
                "version": "1.0",
                "port": 80,
                "projects": {},
                "next_available_port": 8800
            }

        with open(self.routing_file, "r") as f:
            return json.load(f)

    def save_routing(self, routing: Dict):
        """Save routing.json"""
        with open(self.routing_file, "w") as f:
            json.dump(routing, f, indent=2)

    def allocate_port(self) -> int:
        """
        Allocate next available backend port

        Returns:
            Port number (e.g., 8800, 8801, ...)
        """
        routing = self.load_routing()
        port = routing["next_available_port"]

        # Verify port is actually free
        while not self.is_port_free(port):
            port += 1

        # Update next_available_port
        routing["next_available_port"] = port + 1
        self.save_routing(routing)

        return port

    @staticmethod
    def is_port_free(port: int) -> bool:
        """Check if port is available"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def register_project(
        self,
        project_name: str,
        project_path: Path,
        backend_port: int,
        domain: Optional[str] = None
    ) -> Dict:
        """
        Register a project in routing.json

        Args:
            project_name: Unique project name
            project_path: Absolute path to project
            backend_port: Backend port (e.g., 8800)
            domain: Domain name (defaults to {project_name}.dango)

        Returns:
            Project registration dict
        """
        routing = self.load_routing()

        domain = domain or f"{project_name}.dango"

        project_info = {
            "domain": domain,
            "backend_port": backend_port,
            "project_path": str(project_path.absolute()),
            "registered_at": datetime.now().isoformat(),
            "status": "running"
        }

        routing["projects"][project_name] = project_info
        self.save_routing(routing)

        return project_info

    def unregister_project(self, project_name: str):
        """Remove project from routing.json"""
        routing = self.load_routing()
        if project_name in routing["projects"]:
            del routing["projects"][project_name]
            self.save_routing(routing)

    def update_project_status(self, project_name: str, status: str):
        """Update project status (running/stopped)"""
        routing = self.load_routing()
        if project_name in routing["projects"]:
            routing["projects"][project_name]["status"] = status
            self.save_routing(routing)

    def get_project_info(self, project_name: str) -> Optional[Dict]:
        """Get project information"""
        routing = self.load_routing()
        return routing["projects"].get(project_name)

    def list_projects(self) -> Dict[str, Dict]:
        """List all registered projects"""
        routing = self.load_routing()
        return routing["projects"]


class NginxManager:
    """
    Manages shared nginx instance

    Responsibilities:
    - Generate nginx configs
    - Start/stop/reload nginx
    - Health checks
    """

    def __init__(self, config: NetworkConfig):
        self.config = config
        self.nginx_conf = self.config.shared_nginx_dir / "nginx.conf"
        self.nginx_pid = self.config.shared_nginx_dir / "nginx.pid"

    def generate_base_config(self) -> str:
        """Generate base nginx.conf"""
        conf = f"""
# Dango Shared nginx Configuration
# Auto-generated - do not edit manually

worker_processes 1;
error_log {self.config.logs_dir}/error.log;
pid {self.nginx_pid};

events {{
    worker_connections 1024;
}}

http {{
    access_log {self.config.logs_dir}/access.log;

    # Default server (fallback)
    server {{
        listen 80 default_server;
        server_name _;

        location / {{
            return 404 "Dango project not found. Check your URL.";
        }}
    }}

    # Include all project sites
    include {self.config.sites_dir}/*.conf;
}}
"""
        return conf.strip()

    def generate_project_config(
        self,
        project_name: str,
        domain: str,
        backend_port: int,
        metabase_port: int = 3000,
        dbt_docs_port: int = 8080
    ) -> str:
        """
        Generate nginx site config for a project

        Args:
            project_name: Project name
            domain: Domain (e.g., my-analytics.dango)
            backend_port: Backend web UI port
            metabase_port: Metabase port (default 3000)
            dbt_docs_port: dbt docs port (default 8080)
        """
        conf = f"""
# Dango Project: {project_name}
# Domain: {domain}
# Auto-generated

server {{
    listen 80;
    server_name {domain};

    # Dashboard (Web UI frontend)
    location / {{
        proxy_pass http://127.0.0.1:{backend_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }}

    # Metabase
    location /metabase/ {{
        proxy_pass http://127.0.0.1:{metabase_port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}

    # dbt Docs
    location /docs/ {{
        proxy_pass http://127.0.0.1:{dbt_docs_port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}

    # API
    location /api/ {{
        proxy_pass http://127.0.0.1:{backend_port}/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
}}
"""
        return conf.strip()

    def write_base_config(self):
        """Write base nginx.conf"""
        config = self.generate_base_config()
        with open(self.nginx_conf, "w") as f:
            f.write(config)

    def write_project_config(
        self,
        project_name: str,
        domain: str,
        backend_port: int
    ):
        """Write project-specific nginx site config"""
        config = self.generate_project_config(project_name, domain, backend_port)
        site_conf = self.config.sites_dir / f"{project_name}.conf"
        with open(site_conf, "w") as f:
            f.write(config)

    def remove_project_config(self, project_name: str):
        """Remove project nginx site config"""
        site_conf = self.config.sites_dir / f"{project_name}.conf"
        if site_conf.exists():
            site_conf.unlink()

    def is_running(self) -> bool:
        """Check if nginx is running"""
        if not self.nginx_pid.exists():
            return False

        try:
            with open(self.nginx_pid, "r") as f:
                pid = int(f.read().strip())

            # Check if process exists
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def start(self) -> Tuple[bool, str]:
        """
        Start nginx

        Returns:
            Tuple of (success, message)
        """
        # Check if already running
        if self.is_running():
            return True, "nginx already running"

        # Check if port 80 available
        if not NetworkConfig.is_port_free(80):
            return False, "Port 80 in use by another service"

        # Write base config if doesn't exist
        if not self.nginx_conf.exists():
            self.write_base_config()

        # Start nginx
        try:
            result = subprocess.run(
                ["nginx", "-c", str(self.nginx_conf)],
                capture_output=True,
                text=True,
                check=True
            )
            return True, "nginx started successfully"
        except subprocess.CalledProcessError as e:
            return False, f"Failed to start nginx: {e.stderr}"
        except FileNotFoundError:
            return False, "nginx not installed. Install with: brew install nginx (macOS) or apt-get install nginx (Linux)"

    def stop(self) -> Tuple[bool, str]:
        """
        Stop nginx

        Returns:
            Tuple of (success, message)
        """
        if not self.is_running():
            return True, "nginx not running"

        try:
            with open(self.nginx_pid, "r") as f:
                pid = int(f.read().strip())

            os.kill(pid, 15)  # SIGTERM
            return True, "nginx stopped"
        except Exception as e:
            return False, f"Failed to stop nginx: {e}"

    def reload(self) -> Tuple[bool, str]:
        """
        Reload nginx configuration

        Returns:
            Tuple of (success, message)
        """
        if not self.is_running():
            return self.start()

        try:
            result = subprocess.run(
                ["nginx", "-s", "reload", "-c", str(self.nginx_conf)],
                capture_output=True,
                text=True,
                check=True
            )
            return True, "nginx reloaded"
        except subprocess.CalledProcessError as e:
            return False, f"Failed to reload nginx: {e.stderr}"


class HostsManager:
    """
    Manages /etc/hosts entries for .dango domains

    Responsibilities:
    - Add/remove domains from /etc/hosts
    - Backup before modification
    - Handle sudo prompts
    """

    HOSTS_FILE = Path("/etc/hosts")
    BEGIN_MARKER = "# BEGIN DANGO MANAGED HOSTS"
    END_MARKER = "# END DANGO MANAGED HOSTS"

    def __init__(self, config: NetworkConfig):
        self.config = config
        self.backup_file = self.config.dango_home / "hosts-backup"

    def backup_hosts(self):
        """Backup /etc/hosts before first modification"""
        if not self.backup_file.exists():
            with open(self.HOSTS_FILE, "r") as f:
                content = f.read()
            with open(self.backup_file, "w") as f:
                f.write(content)

    def get_dango_domains(self) -> List[str]:
        """Get list of domains currently in /etc/hosts"""
        try:
            with open(self.HOSTS_FILE, "r") as f:
                lines = f.readlines()

            in_section = False
            domains = []

            for line in lines:
                if self.BEGIN_MARKER in line:
                    in_section = True
                    continue
                elif self.END_MARKER in line:
                    break
                elif in_section and line.strip():
                    # Parse: 127.0.0.1  my-analytics.dango
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].endswith(".dango"):
                        domains.append(parts[1])

            return domains
        except Exception:
            return []

    def add_domain(self, domain: str) -> Tuple[bool, str]:
        """
        Add domain to /etc/hosts

        Args:
            domain: Domain name (e.g., my-analytics.dango)

        Returns:
            Tuple of (success, message)
        """
        # Backup first
        self.backup_hosts()

        try:
            with open(self.HOSTS_FILE, "r") as f:
                content = f.read()

            # Check if domain already exists
            if domain in content:
                return True, f"Domain {domain} already in /etc/hosts"

            # Find or create Dango section
            if self.BEGIN_MARKER not in content:
                # Add new section
                new_section = f"\n{self.BEGIN_MARKER}\n127.0.0.1  {domain}\n{self.END_MARKER}\n"
                new_content = content + new_section
            else:
                # Insert into existing section
                lines = content.split("\n")
                new_lines = []
                for line in lines:
                    new_lines.append(line)
                    if self.BEGIN_MARKER in line:
                        new_lines.append(f"127.0.0.1  {domain}")
                new_content = "\n".join(new_lines)

            # Write back (requires sudo)
            temp_file = self.config.dango_home / "hosts-temp"
            with open(temp_file, "w") as f:
                f.write(new_content)

            # Use sudo to copy
            result = subprocess.run(
                ["sudo", "cp", str(temp_file), str(self.HOSTS_FILE)],
                capture_output=True,
                text=True
            )

            temp_file.unlink()

            if result.returncode == 0:
                return True, f"Added {domain} to /etc/hosts"
            else:
                return False, f"Failed to modify /etc/hosts: {result.stderr}"

        except Exception as e:
            return False, f"Failed to add domain: {e}"

    def remove_domain(self, domain: str) -> Tuple[bool, str]:
        """Remove domain from /etc/hosts"""
        try:
            with open(self.HOSTS_FILE, "r") as f:
                lines = f.readlines()

            new_lines = []
            for line in lines:
                if domain not in line or self.BEGIN_MARKER in line or self.END_MARKER in line:
                    new_lines.append(line)

            temp_file = self.config.dango_home / "hosts-temp"
            with open(temp_file, "w") as f:
                f.writelines(new_lines)

            result = subprocess.run(
                ["sudo", "cp", str(temp_file), str(self.HOSTS_FILE)],
                capture_output=True,
                text=True
            )

            temp_file.unlink()

            if result.returncode == 0:
                return True, f"Removed {domain} from /etc/hosts"
            else:
                return False, f"Failed to modify /etc/hosts: {result.stderr}"

        except Exception as e:
            return False, f"Failed to remove domain: {e}"
