"""dango/platform/cloud/_server_templates.py

Config file templates for server setup (server_setup.py) and scheduled
backup (scheduled_backup.py).

String constants and builder functions for remote host configuration.
Extracted from server_setup.py to keep that module under 500 lines.
"""

from __future__ import annotations

import re

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


# The unauthenticated Marimo WebSocket handle written by build_caddyfile()
# up to 1.0.13.  ``remote upgrade`` replaces exactly this text in place.
LEGACY_MARIMO_WS_RE = re.compile(
    r"\thandle /notebooks/marimo/ws \{\n\t\treverse_proxy localhost:(\d+)\n\t\}"
)


def marimo_ws_block(marimo_port: int) -> str:
    """Return the Caddy ``handle`` block for the Marimo WebSocket.

    Marimo runs with ``--no-token``, so Caddy asks dango-web
    (``/api/notebooks/ws-auth``, ``notebooks.execute``) before proxying.
    The WebSocket upgrade headers are stripped from the auth sub-request
    only: a plain HTTP route answers 403 to a request carrying them.
    """
    return (
        "\thandle /notebooks/marimo/ws {\n"
        "\t\tforward_auth localhost:8800 {\n"
        "\t\t\turi /api/notebooks/ws-auth\n"
        "\t\t\theader_up -Connection\n"
        "\t\t\theader_up -Upgrade\n"
        "\t\t\theader_up -Sec-WebSocket-Key\n"
        "\t\t\theader_up -Sec-WebSocket-Version\n"
        "\t\t\theader_up -Sec-WebSocket-Extensions\n"
        "\t\t}\n"
        f"\t\treverse_proxy localhost:{marimo_port}\n"
        "\t}"
    )


def build_caddyfile(domain: str | None = None, marimo_port: int = 7805) -> str:
    """Build a Caddyfile for Caddy reverse proxy.

    Args:
        domain: FQDN for HTTPS (e.g. ``"app.example.com"``).
            When ``None``, generates an HTTP-only config on port 80.
        marimo_port: Port the Marimo notebook server listens on (default 7805).

    Returns:
        Complete Caddyfile content as a string.
    """
    # Marimo WebSocket proxy — Caddy handles WS natively, after a dango-web
    # permission check (forward_auth).  HTTP requests go through dango-web.
    marimo_ws = marimo_ws_block(marimo_port)

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

SYSTEMD_BACKUP_SERVICE = """\
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
