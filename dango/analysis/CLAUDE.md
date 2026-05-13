# analysis/

## Purpose

Automated monitoring and comparison engine. Executes user-defined monitors against DuckDB, stores results in SQLite (`metric_history`, `metric_results` tables in `.dango/dango.db`), and compares current values against historical baselines to detect changes and trends. Pre-built templates auto-generate monitors for common sources.

**Naming:** Config models use "monitor" terminology (`MonitorConfig`, `MonitorsConfig`, `.dango/monitors.yml`). Runtime models retain "metric" terminology (`MetricValue`, `ComparisonResult`). Old names (`MetricConfig`, `MetricsConfig`, `warn_threshold`, `metrics.yml`) are supported via backward-compatible aliases.

## Files

| File | Purpose | Key Exports |
|------|---------|-------------|
| `__init__.py` | Public API re-exports | `run_analysis`, `load_monitors_config`, `save_monitors_config`, `add_monitors_to_config`, `generate_metrics_for_source`, `MonitorConfig`, `MonitorsConfig`, `DimensionContributor`, `DrillDownDimension` + backward-compat aliases |
| `models.py` | Pydantic V2 models. `MonitorConfig.compare` is `ComparisonType | None` (None skips comparison — used for freshness metrics) | `ComparisonType`, `MonitorConfig` (alias: `MetricConfig`), `MonitorsConfig` (alias: `MetricsConfig`), `MetricValue`, `ComparisonResult`, `DimensionContributor`, `DrillDownDimension`, `AnalysisResult` |
| `config.py` | YAML config load/save | `load_monitors_config()`, `save_monitors_config()`, `add_monitors_to_config()`, `get_monitors_file_path()` + backward-compat aliases |
| `comparisons.py` | Comparison engine + trend detection | `compute_comparison()`, `detect_trend()` |
| `drilldown.py` | Drill-down engine: GROUP BY breakdown + contributor ranking | `run_drill_down()` |
| `metrics.py` | Orchestration: execute → store → compare → drill-down | `run_analysis()` |
| `templates.py` | Pre-built monitor templates for common sources | `generate_metrics_for_source()` |
| `formatter.py` (144 lines) | Result categorization + display formatting | `categorize_results()`, status labels, trend direction |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new comparison type | `models.py` (`ComparisonType` enum) + `comparisons.py` (`_get_baseline`) | `pytest tests/unit/test_analysis_comparisons.py` |
| Change monitor validation | `models.py` (`MonitorConfig` validators) | `pytest tests/unit/test_analysis_models.py` |
| Change config file format | `config.py` | `pytest tests/unit/test_analysis_config.py` |
| Save/merge monitors config | `config.py` (`save_monitors_config`, `add_monitors_to_config`) | `pytest tests/unit/test_analysis_config.py` |
| Add source template | `templates.py` (add generator + register in dispatch dict) | `pytest tests/unit/test_analysis_templates.py` |
| Modify trend detection | `comparisons.py` (`detect_trend`, `_linear_regression`) | `pytest tests/unit/test_analysis_comparisons.py` |
| Change metric execution | `metrics.py` (`_execute_metric`, `_build_metric_sql`) | `pytest tests/unit/test_analysis_metrics.py` |
| Add/modify drill-down logic | `drilldown.py` (`run_drill_down`, `_compute_contributors`) | `pytest tests/unit/test_analysis_drilldown.py` |

## Dependencies

**Imports from (dango modules):**
- `exceptions` — `AnalysisConfigError` (config.py)
- `logging` — `get_logger` (all files)
- `utils.dango_db` — `connect()` (comparisons.py, drilldown.py, metrics.py)

**External packages:** `pydantic`, `yaml`, `duckdb`

**Used by:**
- `metrics.py` — calls `run_drill_down()` from `drilldown.py` when threshold exceeded
- `utils/post_sync.py` — `_run_analysis()` calls `run_analysis` with `raw_`-prefixed source filter
- `cli/source_wizard.py` — `generate_metrics_for_source()` + `add_monitors_to_config()` on source add
- `cli/init.py` — `save_monitors_config()` + `generate_metrics_for_source()` on project init
- `web/routes/monitoring.py` — GET /api/monitoring, POST /api/monitoring/run, GET /api/monitoring/history
- `cli/commands/analyze.py` — `dango monitor run` command (+ `dango analyze` alias)

## Testing

```
pytest tests/unit/test_analysis_models.py tests/unit/test_analysis_config.py \
  tests/unit/test_analysis_comparisons.py tests/unit/test_analysis_metrics.py \
  tests/unit/test_analysis_drilldown.py tests/unit/test_analysis_templates.py -v
pytest tests/integration/test_analysis_integration.py -v
```

## Don't Modify

| Item | Reason |
|------|--------|
| `models.py` runtime model field names | P7-010, P7-011, P7-012 depend on exact model shapes |
| `ComparisonType` enum values | Stored in `metric_results.result_type` column |
| `metric_history` / `metric_results` table schemas | Defined in `utils/dango_db.py`, shared across modules |
