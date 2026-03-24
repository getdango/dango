# PostgreSQL

**Category:** Databases | **Auth:** Connection URL | **Wizard:** Yes

## Setup

1. Ensure your PostgreSQL instance is accessible from the machine running Dango
2. Create a read-only user (recommended):
   ```sql
   CREATE USER dango_reader WITH PASSWORD 'your-password';
   GRANT CONNECT ON DATABASE mydb TO dango_reader;
   GRANT USAGE ON SCHEMA public TO dango_reader;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO dango_reader;
   ```
3. Run `dango source add`, select **PostgreSQL**, and enter the connection URL

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `credentials_env` | Yes | Connection URL (env var: `POSTGRES_CREDENTIALS`), e.g., `postgresql://user:pass@host:5432/dbname` |
| `schema` | No | Schema name (default: `public`) |
| `table_names` | No | Tables to sync (empty = all tables) |

**Pip dependencies:** `sqlalchemy`, `psycopg2-binary` (installed automatically)

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
- Connection URL is stored in `.env` as an environment variable
