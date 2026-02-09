# ADR-006: Caddy for SSL

## Status
Accepted

## Context
Dango's cloud deployment needs HTTPS for security (encrypted credentials, session cookies) and for Metabase SSO (the `metabase.SESSION` cookie requires `Secure=true` in production). A reverse proxy is also needed to route requests to Dango (port 8800) and Metabase (port 3000) under a single domain, which eliminates CORS and cookie-domain issues for session bridging (see ADR-001).

## Decision
Use Caddy as the reverse proxy with automatic Let's Encrypt SSL certificate provisioning. Caddy handles HTTPS termination, automatic certificate renewal, and request routing to Dango and Metabase on their respective ports. All services run on the same domain.

## Rationale
- **Zero-config HTTPS:** Caddy automatically obtains Let's Encrypt certificates when given a domain name. No manual certificate generation, no cron jobs for renewal, no configuration of certificate paths. This reduces deployment steps and eliminates a common source of production outages (expired certificates).
- **Same-domain architecture:** Reverse-proxying both Dango and Metabase behind Caddy on the same domain (e.g., `dango.example.com` and `dango.example.com/metabase/`) means cookies set by Dango are sent to Metabase requests. This is critical for session bridging — the `metabase.SESSION` cookie works naturally without cross-domain configuration.
- **Simple configuration:** A Caddy config for Dango's routing is approximately 10 lines. The Caddyfile format is human-readable and easy to generate programmatically during `dango deploy`.
- **Automatic HTTPS redirect:** Caddy redirects HTTP to HTTPS by default. No additional configuration needed.

## Alternatives Considered
- **nginx + certbot:** The most common reverse proxy and SSL combination. However, nginx configuration is more verbose and error-prone for users unfamiliar with it. Certbot requires a separate installation, a cron job for certificate renewal, and post-renewal hook to reload nginx. This is well-documented but adds operational steps that Caddy eliminates entirely.
- **Traefik:** A modern reverse proxy designed for container orchestration (Docker Swarm, Kubernetes). Supports automatic certificate provisioning via Let's Encrypt. However, Traefik's configuration model (labels, dynamic routing, middleware chains) is designed for multi-service container environments — more complexity than needed for Dango's two-service setup.
- **No reverse proxy (direct HTTPS on application):** FastAPI can serve HTTPS directly with `uvicorn --ssl-keyfile --ssl-certfile`. However, this requires manual certificate management, does not handle certificate renewal, and means Metabase must run on a separate port (breaking same-domain session bridging) or behind a path-based routing solution.

## Consequences
- Caddy binary adds ~40MB to the deployment. This is a small fraction of the total deployment size (Metabase alone is ~300MB).
- Requires ports 80 and 443 to be open on the server. Port 80 is needed for the ACME HTTP-01 challenge (Let's Encrypt domain verification) and for the HTTP-to-HTTPS redirect.
- Let's Encrypt rate limits apply: 50 certificates per registered domain per week. This is not a concern for single deployments but would matter if Dango were deployed at scale under a shared domain.
- Caddy's configuration is less familiar to most operations teams than nginx. However, Dango generates the Caddyfile during deployment, so users rarely need to edit it directly.
- If Let's Encrypt is unreachable during deployment (rare), Caddy will retry automatically. The site will be temporarily unavailable over HTTPS until a certificate is obtained.
