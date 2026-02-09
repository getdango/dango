# ADR-003: DuckDB as Single-File Warehouse

## Status
Accepted

## Context
Dango needs an analytical data warehouse to store ingested data (from dlt) and serve transformed data (via dbt) to Metabase dashboards, Marimo notebooks, and the web UI. The warehouse must handle analytical queries (aggregations, joins, window functions) on datasets up to millions of rows while fitting into a single-server deployment.

## Decision
Use DuckDB as an embedded, single-file warehouse. Enforce a single-writer-process constraint: only one process holds a read-write connection at a time (the Dango server), while Metabase, notebooks, and other consumers use read-only connections.

After write operations (dlt sync, dbt run), issue a `CHECKPOINT` to flush the write-ahead log and ensure durability.

## Rationale
VAL-003 validated DuckDB 1.4.4 against Dango's requirements:

- **Read performance:** 249.5 queries/sec with 10 concurrent readers, 0.015s average for `GROUP BY` over 1M rows. Analytical query performance far exceeds SQLite.
- **Concurrent reads during writes:** Read-only connections work without errors during active writes (max read latency 0.007s during writes). DuckDB's MVCC provides consistent snapshots.
- **Memory efficiency:** 112 MB peak for 10 in-process cursors, 467 MB for 10 cross-process readers. Well within a 4GB droplet.
- **No server process:** DuckDB is an embedded library. No database server to configure, monitor, or restart. The warehouse is a single `.duckdb` file.
- **dbt integration:** The `dbt-duckdb` adapter is mature and handles DuckDB's SQL dialect.

## Alternatives Considered
- **PostgreSQL:** Proven analytical database with no single-writer limitation, replication, and a mature ecosystem. However, running a PostgreSQL server adds significant operational complexity for small teams: memory overhead (~100MB baseline), configuration tuning, backup management, and monitoring. The small-team target audience should not need to manage a database server.
- **SQLite:** Embedded and single-file like DuckDB, but optimized for transactional (OLTP) workloads, not analytical queries. SQLite lacks columnar storage, vectorized execution, and analytical SQL functions that DuckDB provides. Aggregation queries on large datasets would be significantly slower.

## Consequences
- **Single-writer-process:** Only one process can hold a read-write connection. All write jobs (dlt sync, dbt run, dbt test) must be serialized — never run in parallel. The scheduler (ADR-002) enforces this via `max_instances=1` and sequential task chains.
- **Read-only connections for consumers:** Metabase, Marimo notebooks, and web UI queries connect with `read_only=True`. This works during writes but means consumers cannot create temporary tables or write to the warehouse.
- **File-level locking:** DuckDB v1.4 uses file-level locks. A read-write connection from one process blocks read-only connections from other OS processes (though in-process read-only cursors work fine). Notebooks must query between syncs or use a snapshot copy (see ADR-004).
- **No replication:** If the server disk fails, the warehouse must be rebuilt from sources (re-run dlt syncs). Backups of the `.duckdb` file are essential.
- **Dataset size limit:** DuckDB performs best with datasets that fit in memory. For the target scale (millions of rows, GBs of data), this is not a concern on a 4GB droplet. Datasets exceeding available RAM will still work but with reduced query performance.
