# MongoDB

**Category:** Databases | **Auth:** Connection URL | **Wizard:** Yes

## Setup

1. Ensure your MongoDB instance is accessible
2. Create a read-only user (recommended):
   ```javascript
   db.createUser({
     user: "dango_reader",
     pwd: "your-password",
     roles: [{ role: "read", db: "mydb" }]
   })
   ```
3. Run `dango source add`, select **MongoDB**, and enter the connection URL

## Configuration

| Parameter | Required | Description |
|-----------|----------|-------------|
| `connection_url_env` | Yes | Connection URL (env var: `MONGODB_CONNECTION_URL`), e.g., `mongodb://user:pass@host:27017/mydb` |
| `database` | No | Database name (empty = from connection URL) |
| `collection_names` | No | Collections to sync (empty = all) |
| `parallel` | No | Enable parallel loading (default: false) |

**Pip dependency:** `pymongo` (installed automatically)

## Known Limitations

- Wizard flow verified; real sync not tested in Phase 5
- Incremental loading supported
