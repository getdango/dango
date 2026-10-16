"""dango/platform/cloud/_server_templates.py

Config file templates for server setup (server_setup.py) and scheduled
backup (scheduled_backup.py).

String constants and builder functions for remote host configuration.
Extracted from server_setup.py to keep that module under 500 lines.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Security headers shared by HTTP and HTTPS Caddyfile configurations
# ---------------------------------------------------------------------------

_COMMON_HEADERS = """\
\theader {
\t\tX-Content-Type-Options nosniff
\t\tX-Frame-Options SAMEORIGIN
\t\tReferrer-Policy strict-origin-when-cross-origin
\t\tPermissions-Policy "interest-cohort=()"
\t}"""

_HSTS_HEADER = '\t\tStrict-Transport-Security "max-age=63072000; includeSubDomains"'


def build_caddyfile(domain: str | None = None, marimo_port: int = 7805) -> str:
    """Build a Caddyfile for Caddy reverse proxy.

    Args:
        domain: FQDN for HTTPS (e.g. ``"app.example.com"``).
            When ``None``, generates an HTTP-only config on port 80.
        marimo_port: Port the Marimo notebook server listens on (default 7805).

    Returns:
        Complete Caddyfile content as a string.
    """
    # Marimo WebSocket proxy — Caddy handles WS natively.
    # HTTP requests still go through dango-web (auth-protected).
    marimo_ws = f"\thandle /notebooks/marimo/ws {{\n\t\treverse_proxy localhost:{marimo_port}\n\t}}"

    if domain:
        # HTTPS mode — Caddy auto-obtains Let's Encrypt certs
        headers = _COMMON_HEADERS.replace(
            "\n\t}",
            f"\n{_HSTS_HEADER}\n\t}}",
        )
        domain_block = f"{domain} {{\n{marimo_ws}\n\thandle {{\n\t\treverse_proxy localhost:8800\n\t}}\n{headers}\n}}\n"
        # Block direct IP access — prevent unencrypted credential exposure
        ip_block = ':80 {\n\trespond "Use the configured domain to access this server." 403\n}\n'
        return f"{domain_block}\n{ip_block}"
    # HTTP-only mode (IP access, no HSTS)
    return f":80 {{\n{marimo_ws}\n\thandle {{\n\t\treverse_proxy localhost:8800\n\t}}\n{_COMMON_HEADERS}\n}}\n"


# Backward-compatible alias — existing code imports ``CADDYFILE`` directly.
CADDYFILE = build_caddyfile()


def build_systemd_unit(workers: int | None = None) -> str:
    """Build a systemd unit file for the Dango web service.

    Args:
        workers: Number of uvicorn workers. When > 1, adds ``--workers N``
            to the ExecStart line. When ``None`` or 1, uses the default
            single-worker mode.

    Returns:
        Complete systemd unit file content as a string.
    """
    if workers is not None and workers > 1:
        exec_start = f"/srv/dango/venv/bin/dango serve --workers {workers}"
    else:
        exec_start = "/srv/dango/venv/bin/dango serve"

    return f"""\
[Unit]
Description=Dango Web Platform
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=dango
Group=dango
WorkingDirectory=/srv/dango/project
Environment=DLT_DATA_DIR=/srv/dango/project/.dlt
Environment=DANGO_CLOUD_MODE=true
Environment=DANGO_SKIP_ENSURE_ADMIN=true
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


# Backward-compatible alias — existing code imports ``SYSTEMD_UNIT`` directly.
SYSTEMD_UNIT = build_systemd_unit()


DOCKER_DAEMON_JSON = """\
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
"""

JOURNALD_CONF = """\
[Journal]
SystemMaxUse=500M
"""

LOGROTATE_CONF = """\
/srv/dango/project/.dango/logs/activity.log {
    su dango dango
    size 10M
    rotate 3
    compress
    copytruncate
    missingok
    notifempty
}
"""

FAIL2BAN_JAIL = """\
[sshd]
enabled = true
port = ssh
filter = sshd
backend = systemd
maxretry = 5
bantime = 3600
"""

UNATTENDED_UPGRADES_CONF = """\
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}";
    "${distro_id}:${distro_codename}-security";
    "${distro_id}ESMApps:${distro_codename}-apps-security";
    "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
"""

# ---------------------------------------------------------------------------
# Scheduled backup (TASK-103)
# ---------------------------------------------------------------------------

# Runs as root: the project ``.env`` is never loaded as environment (the backup
# reads only the Spaces keys from it, as data), ``-I`` keeps the working
# directory and ``PYTHON*`` variables out of the interpreter, and the working
# directory is outside the dango-writable project tree.

SYSTEMD_BACKUP_SERVICE = """\
[Unit]
Description=Dango Scheduled Backup
After=network.target docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/srv/dango
ExecStart=/srv/dango/venv/bin/python -I -m dango.platform.cloud.scheduled_backup
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
"""

# Unit for servers still running Dango <= 1.0.13, whose scheduled backup reads the
# Spaces keys from its environment. Written only to such servers, with a warning.
SYSTEMD_BACKUP_SERVICE_LEGACY = """\
[Unit]
Description=Dango Scheduled Backup
After=network.target docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=/srv/dango/project
EnvironmentFile=-/srv/dango/project/.env
ExecStart=/srv/dango/venv/bin/python -m dango.platform.cloud.scheduled_backup
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
"""

#: First release whose scheduled backup reads the Spaces keys from ``.env`` itself.
BACKUP_UNIT_MIN_RELEASE = (1, 0, 14)

#: Prints the Dango version installed on the server (run as root over SSH).
SERVER_DANGO_VERSION_COMMAND = (
    '/srv/dango/venv/bin/python -I -c "import dango; print(dango.__version__)"'
)


def backup_service_unit(installed_version: str | None) -> tuple[str, str | None]:
    """Return ``(unit, warning)`` for the scheduled backup on a server.

    Servers older than :data:`BACKUP_UNIT_MIN_RELEASE` get the legacy unit (their
    code needs the keys in its environment) plus a warning to upgrade. An
    unreadable version gets the current unit and a warning.
    """
    from packaging.version import InvalidVersion, Version

    hint = "Run `dango remote upgrade`, then `dango remote backup enable` again."
    try:
        release = Version(installed_version or "").release
    except InvalidVersion:
        return (
            SYSTEMD_BACKUP_SERVICE,
            "Could not read the Dango version on the server; if it is older than "
            f"1.0.14, scheduled uploads to Spaces will fail. {hint}",
        )
    if release < BACKUP_UNIT_MIN_RELEASE:
        return (
            SYSTEMD_BACKUP_SERVICE_LEGACY,
            f"The server runs Dango {installed_version}; it gets the previous backup unit, "
            f"which loads the project .env into a root service. {hint}",
        )
    return SYSTEMD_BACKUP_SERVICE, None


SYSTEMD_BACKUP_TIMER = """\
[Unit]
Description=Dango Daily Backup Timer

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
"""
