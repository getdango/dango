# templates/

## Purpose

Jinja2 templates and Dockerfiles used by CLI project scaffolding and dbt model generation.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Package marker | — |
| `docker-compose.yml.j2` | Jinja2 template for Docker Compose config | Rendered by `cli/init.py` |
| `Dockerfile.metabase` | Metabase container image | Copied by `cli/init.py` |
| `entrypoint.sh` | Metabase container entrypoint — fixes bind-mounted plugin dir ownership, drops to metabase user via gosu | Copied by `cli/init.py` |
| `nginx.conf.j2` | Jinja2 template for nginx reverse proxy config | Not yet consumed (placeholder) |
| `dbt/sources.yml.j2` | dbt source definition template | Rendered by `transformation/generator.py` |
| `dbt/staging_model.sql.j2` | dbt staging model SQL template | Rendered by `transformation/generator.py` |
| `dbt/staging_schema.yml.j2` | dbt staging schema YAML template | Rendered by `transformation/generator.py` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change Docker Compose structure | `docker-compose.yml.j2` | `dango init` in a test project |
| Change Metabase container setup | `Dockerfile.metabase`, `entrypoint.sh` | `docker build -f Dockerfile.metabase .` |
| Change generated dbt model SQL | `dbt/staging_model.sql.j2` | Manual: run `dango sync` and inspect output |
| Change generated dbt schema | `dbt/staging_schema.yml.j2` | Manual: run `dango sync` and inspect output |
| Add a new dbt template | `dbt/` + `transformation/generator.py` | Manual: run `dango sync` and inspect output |

## Dependencies

**Imports from:**
- None (templates are static files, no Python imports)

**Used by:**
- `cli/init.py` — renders `docker-compose.yml.j2`, copies `Dockerfile.metabase` and `entrypoint.sh` during project scaffolding (via `jinja2.PackageLoader('dango', 'templates')`)
- `transformation/generator.py` — renders `dbt/*.j2` templates for dbt model generation (via `jinja2.FileSystemLoader`)

## Testing

- **Unit:** No dedicated test file yet (templates are static files; dbt rendering tested via `transformation/` tests when available)
- **Integration:** `dango init` in a temp directory, verify generated files
- **Manual:** Inspect generated `docker-compose.yml` and dbt models after `dango init` + `dango sync`

## Notes

- **Cloud server config templates** (systemd unit, Caddyfile, fail2ban, etc.) are NOT in this directory. They are embedded as string constants in `platform/cloud/_server_templates.py` because they are not Jinja2 templates — they are plain config files written verbatim to the remote host.

## Don't Modify

| File | Reason |
|------|--------|
| Template variable names (e.g. `{{ project_name }}`) | Changing variables breaks `cli/init.py` and `transformation/generator.py` rendering |
