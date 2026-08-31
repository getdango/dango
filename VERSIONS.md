# Version Stack

Dango bundles several tightly coupled data tools. This document defines
the tested stack and upgrade process.

## Current Tested Stack

| Component | Tested Version | Allowed Range | Source of Truth |
|-----------|---------------|---------------|-----------------|
| DuckDB (Python) | 1.5.4 | >=1.5.0,<1.6 | `pyproject.toml` |
| Metabase JDBC Driver | 1.5.4.0 | pinned | `dango/utils/driver.py` |
| Metabase | v0.62.18 | pinned | `dango/templates/Dockerfile.metabase` |
| dbt-core | 1.11.14 | ~=1.11.0 | `pyproject.toml` |
| dbt-duckdb | 1.11.0 | >=1.11.0,<1.12 | `pyproject.toml` |
| dlt | 1.28.1 | ~=1.28.0 | `pyproject.toml` |

Tested versions are recorded in `constraints.txt` for reproducible installs.

## Tier Definitions

### Tier 1 — Tightly Coupled

Must be changed as a group. A mismatch causes data visibility failures.

- **DuckDB Python** and **Metabase JDBC Driver** must share the same
  DuckDB **major.minor**. DuckDB read-only mode (used by Metabase)
  cannot read files written by a different major.minor.
- **dbt-duckdb** must match **dbt-core** major.minor.

### Tier 2 — Semi-Coupled

Patch upgrades are safe. Minor upgrades need testing.

- **dbt-core** — 1.11 introduced two new opt-in behavior flags
  (`require_unique_project_resource_names`,
  `require_ref_searches_node_package_before_root`) that default to `false`;
  no automatic behavior change. The next real compatibility boundary is
  1.12, where those two flags flip to `true` by default.
- **dlt** — generally backwards compatible within a minor series.

### Tier 3 — Utilities

Everything else (`click`, `fastapi`, `pydantic`, etc.). Upgrade freely
within SemVer constraints.

## Upgrade Process (Tier 1)

1. Check the [Metabase DuckDB driver releases](https://github.com/motherduckdb/metabase_duckdb_driver/releases)
   for the driver version matching your target Metabase.
2. Identify the DuckDB major.minor bundled in that driver.
   (The driver's bundled DuckDB version must be checked from the driver
   release notes — it does not always match the Python DuckDB package version.)
3. Update **all three** together:
   - `pyproject.toml`: `duckdb>=X.Y.0,<X.(Y+1)`
   - `dango/utils/driver.py`: `METABASE_DUCKDB_DRIVER_VERSION`
   - `constraints.txt`: exact tested pins
4. Run `pip install -e ".[dev]"` to resolve.
5. Run `pytest tests/unit/ -x` and `pytest tests/integration/test_metabase_duckdb.py -v`.
6. The pre-commit hook `version-alignment` will block commits if
   pyproject.toml and driver.py diverge.

## Known Incompatibilities

- **DuckDB 1.5.x read-only cannot read 1.4.x files.** Write mode can
  (auto-migrates format), but Metabase connects in read-only mode.
- **dbt-core 1.12** will flip `require_unique_project_resource_names` and
  `require_ref_searches_node_package_before_root` to `true` by default.
  Do not upgrade past 1.11.x without a dedicated compatibility effort.

## Guard Rails

- **Startup check:** `dango start` and `dango serve` verify Python
  DuckDB and driver share the same major.minor before downloading the
  driver or starting services.
- **Pre-commit hook:** `scripts/check_version_alignment.py` blocks
  commits that change `pyproject.toml` or `driver.py` if versions
  diverge.
- **Integration test:** `tests/integration/test_metabase_duckdb.py`
  validates the full Python DuckDB → Metabase JDBC read path.
