# analysis/

## Purpose

Automated metric monitoring and comparison engine. Executes user-defined metrics against DuckDB, stores results in SQLite (`metric_history`, `metric_results` tables in `.dango/dango.db`), and compares current values against historical baselines to detect changes and trends. Pre-built templates auto-generate metrics for common sources.

## Files

| File | Purpose | Key Exports |
|------|---------|-------------|
| `__init__.py` | Public API re-exports | `run_analysis`, `load_metrics_config`, `save_metrics_config`, `add_metrics_to_config`, `generate_metrics_for_source`, `DimensionContributor`, `DrillDownDimension` |
| `models.py` | Pydantic V2 models | `ComparisonType`, `MetricConfig`, `MetricsConfig`, `MetricValue`, `ComparisonResult`, `DimensionContributor`, `DrillDownDimension`, `AnalysisResult` |
| `config.py` | YAML config load/save | `load_metrics_config()`, `save_metrics_config()`, `add_metrics_to_config()`, `get_metrics_file_path()` |
| `comparisons.py` | Comparison engine + trend detection | `compute_comparison()`, `detect_trend()` |
| `drilldown.py` | Drill-down engine: GROUP BY breakdown + contributor ranking | `run_drill_down()` |
| `metrics.py` | Orchestration: execute → store → compare → drill-down | `run_analysis()` |
| `templates.py` | Pre-built metric templates for common sources | `generate_metrics_for_source()` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new comparison type | `models.py` (`ComparisonType` enum) + `comparisons.py` (`_get_baseline`) | `pytest tests/unit/test_analysis_comparisons.py` |
| Change metric validation | `models.py` (`MetricConfig` validators) | `pytest tests/unit/test_analysis_models.py` |
| Change config file format | `config.py` | `pytest tests/unit/test_analysis_config.py` |
| Save/merge metrics config | `config.py` (`save_metrics_config`, `add_metrics_to_config`) | `pytest tests/unit/test_analysis_config.py` |
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
- `cli/source_wizard.py` — `generate_metrics_for_source()` + `add_metrics_to_config()` on source add
- `cli/init.py` — `save_metrics_config()` + `generate_metrics_for_source()` on project init
- `web/routes/insights.py` — planned (P7-012)
- `cli/commands/analyze.py` — planned (P7-012)

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
| `models.py` field names | P7-010, P7-011, P7-012 depend on exact model shapes |
| `ComparisonType` enum values | Stored in `metric_results.result_type` column |
| `metric_history` / `metric_results` table schemas | Defined in `utils/dango_db.py`, shared across modules |
