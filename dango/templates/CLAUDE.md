# templates/

## Purpose

Jinja2 templates and Dockerfiles used by CLI project scaffolding and dbt model generation.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Package marker | — |
| `docker-compose.yml.j2` | Jinja2 template for Docker Compose config | Rendered by `cli/init.py` |
| `Dockerfile.metabase` | Metabase container image | Copied by `cli/init.py` |
| `nginx.conf.j2` | Jinja2 template for nginx reverse proxy config | Not yet consumed (placeholder) |
| `dbt/sources.yml.j2` | dbt source definition template | Rendered by `transformation/generator.py` |
| `dbt/staging_model.sql.j2` | dbt staging model SQL template | Rendered by `transformation/generator.py` |
| `dbt/staging_schema.yml.j2` | dbt staging schema YAML template | Rendered by `transformation/generator.py` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change Docker Compose structure | `docker-compose.yml.j2` | `dango init` in a test project |
| Change Metabase container setup | `Dockerfile.metabase` | `docker build -f Dockerfile.metabase .` |
| Change generated dbt model SQL | `dbt/staging_model.sql.j2` | `pytest tests/unit/test_transformation.py` |
| Change generated dbt schema | `dbt/staging_schema.yml.j2` | `pytest tests/unit/test_transformation.py` |
| Add a new dbt template | `dbt/` + `transformation/generator.py` | `pytest tests/unit/test_transformation.py` |

## Dependencies

**Imports from:**
- None (templates are static files, no Python imports)

**Used by:**
- `cli/init.py` — renders `docker-compose.yml.j2` and copies `Dockerfile.metabase` during project scaffolding (via `jinja2.PackageLoader('dango', 'templates')`)
- `transformation/generator.py` — renders `dbt/*.j2` templates for dbt model generation (via `jinja2.FileSystemLoader`)

## Testing

- **Unit:** `pytest tests/unit/test_transformation.py` (covers dbt template rendering)
- **Integration:** `dango init` in a temp directory, verify generated files
- **Manual:** Inspect generated `docker-compose.yml` and dbt models after `dango init` + `dango sync`

## Don't Modify

| File | Reason |
|------|--------|
| Template variable names (e.g. `{{ project_name }}`) | Changing variables breaks `cli/init.py` and `transformation/generator.py` rendering |
