# Deploy to Any Server (BYOS)

Dango can be deployed to any Ubuntu 22.04+ server — not just DigitalOcean. This guide covers the "Bring Your Own Server" (BYOS) deployment path.

## Requirements

- **Ubuntu 22.04+** (tested on 22.04 LTS and 24.04 LTS)
- **Root SSH access** (key-based authentication)
- **2+ GB RAM** (4 GB recommended)
- **Ports 22, 80, 443** open in your provider's firewall/security group

## Quick Start

### Interactive

```bash
dango deploy
# Select: "I already have a server (any cloud provider)"
# Follow the wizard prompts
```

### Non-Interactive

```bash
dango deploy --byos \
  --server-ip 203.0.113.10 \
  --ssh-user root \
  --ssh-key ~/.ssh/id_ed25519 \
  --domain dango.example.com \
  --admin-email admin@example.com \
  --admin-password "YourSecurePassword123!"
```

## What Gets Installed

The BYOS deployment runs the same 16-step server setup as DigitalOcean deployments, plus UFW firewall configuration:

1. System packages (Python, curl, fail2ban, unattended-upgrades)
2. `dango` system user
3. Docker (via `get.docker.com`)
4. Caddy reverse proxy (auto-TLS with Let's Encrypt if domain is set)
5. Python venv with `getdango` installed
6. SSH hardening (password auth disabled)
7. systemd service (`dango-web`)
8. Fail2ban SSH protection
9. Unattended security upgrades
10. **UFW firewall** (SSH + HTTP + HTTPS)

All steps are idempotent — safe to re-run if deployment is interrupted.

## Provider-Specific Tips

### AWS EC2

1. Launch an Ubuntu 22.04 LTS instance (t3.small or larger)
2. Configure Security Group: allow inbound TCP 22, 80, 443
3. Use the EC2 key pair as your `--ssh-key`

### Google Cloud Compute Engine

1. Create a VM with Ubuntu 22.04 LTS image (e2-medium or larger)
2. Add VPC firewall rules: allow TCP 22, 80, 443
3. Add your SSH public key to the VM metadata

### Hetzner / Linode / Vultr / Any VPS

1. Create an Ubuntu 22.04 server (2+ GB RAM)
2. Add your SSH public key during creation
3. Most VPS providers have ports open by default — Dango's UFW handles host-level firewall

## SSH Key Options

The wizard offers two choices:

1. **Use an existing key** — Point to any SSH private key (e.g., `~/.ssh/id_ed25519`)
2. **Generate a new key** — Creates `.dango/cloud_key` and displays the public key for you to add to the server

## Domain & HTTPS

If you configure a domain, Caddy auto-provisions a Let's Encrypt TLS certificate. Point a DNS A record to your server IP before or immediately after deployment.

Without a domain, Dango is accessible at `http://<server-ip>` (HTTP only).

## Post-Deployment

All `dango remote` commands work with BYOS deployments:

```bash
dango remote push           # Push config/dbt changes
dango remote status         # Server health + resource usage
dango remote logs           # View server logs
dango remote ssh            # SSH into the server
dango remote env set K=V    # Set environment variables
dango remote domain set X   # Configure a domain
dango remote upgrade        # Upgrade Dango version
dango remote backup         # On-demand backup (SSH-based)
```

### Commands NOT Available for BYOS

These require DigitalOcean API access:

- `dango remote resize` — Resize through your hosting provider instead
- `dango remote migrate` — Provision a new server and redeploy manually
- `dango remote firewall` — Use `ufw` commands via SSH instead
- `dango remote backup enable/disable` — Spaces-based scheduled backups are DO-only

## Teardown

```bash
dango deploy destroy
```

For BYOS, this:
- Optionally stops Dango services on the remote server
- Removes the local `.dango/cloud.yml` config
- Does **NOT** delete the server itself — manage that through your hosting provider
