# ADR-004: Marimo over JupyterLite

## Status
Accepted

## Context
Dango needs an interactive notebook environment for exploratory data analysis. Users should be able to query the DuckDB warehouse, visualize results, and iterate on analyses. Notebooks must integrate with the existing deployment (single server, no additional services) and produce artifacts that work well with version control.

## Decision
Use Marimo notebooks (`.py` format) for exploratory analysis. Notebooks connect to the DuckDB warehouse via read-only connections. The `dango notebook` command wraps `marimo edit` with the correct database path.

## Rationale
VAL-006 validated Marimo 0.17.6 across 8 test areas:

- **Git-friendly format:** Marimo notebooks are pure `.py` files with `@app.cell` decorators. They are fully diffable in git, compile as valid Python, and execute as standard scripts (`python notebook.py`). This eliminates the merge-conflict pain of JSON-based `.ipynb` files.
- **Reactive execution:** Cells with dependencies execute in the correct order automatically. Changing an upstream cell re-executes all downstream cells — no stale state from out-of-order execution.
- **Query performance:** Aggregation on 100k rows completes in under 5ms. Join + aggregation in 6.4ms. Window functions in 46ms. All well under the 1s target.
- **Script execution:** Notebooks run as standard Python scripts with correct cell dependency resolution and exit code 0. This enables CI testing of notebook code.
- **Visualization:** Matplotlib renders correctly. In the interactive editor, `mo.mpl.interactive(fig)` provides interactive charts.

## Alternatives Considered
- **JupyterLite:** Browser-based Jupyter environment that runs without a server process. However, `.ipynb` files are JSON and produce noisy git diffs. JupyterLite's runtime is heavier and the execution model allows out-of-order cell execution, leading to hidden state bugs. No reactive dependency tracking.
- **Plain Python scripts:** No interactivity. Users would need to re-run the entire script for each change. No cell-based iteration, no inline visualization. Suitable for automation but not exploratory analysis.

## Consequences
- **DuckDB file-level lock:** DuckDB v1.4 does not support concurrent read-only and read-write connections from separate processes. During dlt sync operations, notebook connections will fail with a lock error. Mitigation: notebooks query between syncs (syncs are periodic and complete in seconds to minutes, so notebooks are available the vast majority of the time). For users who need to query during syncs, a snapshot copy of the warehouse file provides a workaround.
- **Deployment size:** The Marimo editor adds ~50MB to the deployment. This is acceptable for a server that already includes DuckDB, dbt, and Metabase.
- **Learning curve:** Marimo's reactive model differs from Jupyter's execute-in-any-order approach. Users familiar with Jupyter need to adjust to explicit cell dependencies via function parameters. The tradeoff is reproducibility — Marimo notebooks always produce the same output regardless of execution order.
- **Ecosystem:** Marimo's extension ecosystem is smaller than Jupyter's. Most data analysis libraries (pandas, matplotlib, plotly) work without issue, but Jupyter-specific widgets and extensions are not compatible.
