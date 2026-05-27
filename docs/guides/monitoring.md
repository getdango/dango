# Monitoring

Dango exposes a public health endpoint for uptime monitoring integration.

## Health Endpoint

```
GET /api/health
```

- **Public** — no authentication required
- **Minimal response** — returns `{"status": "ok"}` (no sensitive info)
- **Load-balancer friendly** — returns 200 when the server is healthy

### Example Response

```json
{
  "status": "ok"
}
```

### Status Codes

| Code | Meaning |
|------|---------|
| 200 | Server is healthy and responding |
| 503 | Server is unhealthy or starting up |

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
