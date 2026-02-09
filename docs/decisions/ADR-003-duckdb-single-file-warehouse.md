# ADR-003: DuckDB as Single-File Warehouse

## Status
Accepted

## Context
Dango needs an analytical data warehouse to store ingested data (from dlt) and serve transformed data (via dbt) to Metabase dashboards, Marimo notebooks, and the web UI. The warehouse must handle analytical queries (aggregations, joins, window functions) on datasets up to millions of rows while fitting into a single-server deployment.

## Decision
Use DuckDB as an embedded, single-file warehouse. Enforce a single-writer-process constraint: only one process holds a read-write connection at a time (the Dango server). Metabase, notebooks, and other consumers use read-only connections, querying between sync operations due to file-level locking (see Consequences).

After write operations (dlt sync, dbt run), issue a `CHECKPOINT` to flush the write-ahead log and ensure durability.

## Rationale
VAL-003 validated DuckDB 1.4.4 performance and VAL-006 confirmed the cross-process file-level locking constraint:

- **Read performance:** 249.5 queries/sec with 10 concurrent readers, 0.015s average for `GROUP BY` over 1M rows. Analytical query performance far exceeds SQLite.
- **In-process read concurrency:** Within a single process, read-only cursors work without errors during active writes (max read latency 0.007s, VAL-003). DuckDB's MVCC provides consistent snapshots for in-process readers. However, VAL-006 confirmed that cross-process read-only connections are blocked by file-level locking when a read-write connection is held — see Consequences.
- **Memory efficiency:** 112 MB peak for 10 in-process cursors, 467 MB for 10 cross-process readers. Well within a 4GB droplet.
- **No server process:** DuckDB is an embedded library. No database server to configure, monitor, or restart. The warehouse is a single `.duckdb` file.
- **dbt integration:** The `dbt-duckdb` adapter is mature and handles DuckDB's SQL dialect.

## Alternatives Considered
- **PostgreSQL:** Proven analytical database with no single-writer limitation, replication, and a mature ecosystem. However, running a PostgreSQL server adds significant operational complexity for small teams: memory overhead (~100MB baseline), configuration tuning, backup management, and monitoring. The small-team target audience should not need to manage a database server.
- **SQLite:** Embedded and single-file like DuckDB, but optimized for transactional (OLTP) workloads, not analytical queries. SQLite lacks columnar storage, vectorized execution, and analytical SQL functions that DuckDB provides. Aggregation queries on large datasets would be significantly slower.

## Consequences
- **Single-writer-process:** Only one process can hold a read-write connection. All write jobs (dlt sync, dbt run, dbt test) must be serialized — never run in parallel. The scheduler (ADR-002) enforces this via `max_instances=1` and sequential task chains.
- **Read-only connections for consumers:** Metabase, Marimo notebooks, and web UI queries connect with `read_only=True`. In-process readers (web UI queries served by the Dango server) work during writes via MVCC. Cross-process readers (Metabase, notebooks) are blocked by file-level locking during writes and must query between syncs or use a snapshot copy (see ADR-004). Consumers cannot create temporary tables or write to the warehouse.
- **File-level locking:** DuckDB v1.4 uses file-level locks (confirmed by VAL-006). A read-write connection from one process blocks all connections — including read-only — from other OS processes. In-process read-only cursors are unaffected. Since dlt and dbt jobs run inside the Dango server process (via APScheduler threads), web UI queries from the same process work during writes, but Metabase (separate Java process) and Marimo notebooks (separate Python process) must wait.
- **No replication:** If the server disk fails, the warehouse must be rebuilt from sources (re-run dlt syncs). Backups of the `.duckdb` file are essential.
- **Dataset size limit:** DuckDB performs best with datasets that fit in memory. For the target scale (millions of rows, GBs of data), this is not a concern on a 4GB droplet. Datasets exceeding available RAM will still work but with reduced query performance.
