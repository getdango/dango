"""dango/platform/cloud/_server_templates.py

Config file templates for server setup (server_setup.py).

These are plain string constants written to the remote host during setup.
Extracted from server_setup.py to keep that module under 500 lines.
"""

from __future__ import annotations

SYSTEMD_UNIT = """\
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
ExecStart=/srv/dango/venv/bin/dango serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

CADDYFILE = """\
:80 {
\treverse_proxy localhost:8800
}
"""

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
