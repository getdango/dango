# Monitoring

Dango exposes a public health endpoint for uptime monitoring integration.

## Health Endpoint

```
GET /api/health
```

- **Public** — no authentication required
- **Dango's own health** — answers from the Dango web server itself, not from Metabase, so a slow or restarting Metabase never makes it fail
- **Coarse states only** — no versions, paths, hostnames or error text
- **Load-balancer friendly** — returns 200 whenever the Dango web server is up (GET only; HEAD and OPTIONS return 405)

### Example Response

```json
{
  "status": "ok",
  "components": {
    "dango": "ok",
    "metabase": "ok",
    "docker": "ok"
  }
}
```

| Field | Values |
|-------|--------|
| `status` | `ok`, or `degraded` when Dango cannot find its project |
| `components.dango` | `ok`, `degraded` |
| `components.metabase` | `ok`, `starting` (Metabase is initializing), `unavailable`, `unknown` |
| `components.docker` | `ok`, `unavailable`, `not_applicable` (cloud servers, where Dango has no Docker access by design), `unknown` |

Component states come from short, cached checks (Metabase about 5 seconds, Docker about 15 seconds), so monitoring this endpoint does not load Metabase or Docker. A component state is information: it never changes the HTTP status. To alert on Metabase, match `"metabase":"ok"` in the body. Signed-in users can still reach Metabase's own health at `/metabase/api/health`.

### Status Codes

| Code | Meaning |
|------|---------|
| 200 | The Dango web server is up and responding |
| other / no response | The Dango web server is down or unreachable |

## Uptime Monitoring Setup

### UptimeRobot (Free)

1. Create a new monitor at [uptimerobot.com](https://uptimerobot.com)
2. Type: HTTP(s)
3. URL: `https://your-domain.com/api/health` (or `http://<ip>/api/health`)
4. Monitoring interval: 5 minutes
5. Alert contacts: your email

### Better Uptime

1. Create a new monitor at [betteruptime.com](https://betteruptime.com)
2. URL: `https://your-domain.com/api/health`
3. Check period: 3 minutes
4. Expected status code: 200

### Generic HTTP Monitor

Any monitoring service that supports HTTP checks works. Configure:

- **URL:** `https://your-domain.com/api/health`
- **Method:** GET
- **Expected status:** 200
- **Timeout:** 10 seconds
- **Interval:** 3–5 minutes

## Platform Health (Authenticated)

For detailed health information (resource usage, service status, backup health), use the authenticated endpoint:

```
GET /api/health/platform
```

This requires a valid session or API key and returns detailed metrics including CPU, memory, disk, Docker status, and backup staleness.

Access this via the Dango web UI at `/health` or programmatically with an API key:

```bash
curl -H "Authorization: Bearer <api-key>" https://your-domain.com/api/health/platform
```
