# security/

## Purpose

Protects OAuth tokens and Metabase administrator credentials from project-local
storage.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `SecureTokenStorage`, `MetabaseCredentialStore` |
| `metabase_credentials.py` | Per-project Metabase admin-password storage outside the project tree | `MetabaseCredentialStore` |
| `metabase_config.py` | Metabase metadata and administrator-credential access boundary | `load_metabase_metadata`, `resolve_metabase_url` (host-side URL: metadata, then configured port, then 3000; tested by `test_resolve_metabase_url.py`), `load_metabase_admin_credentials`, `write_metabase_metadata` |
| `token_storage.py` | Token encryption/decryption with OS keychain key storage | `SecureTokenStorage` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change encryption approach | `token_storage.py` | Manual: encrypt then decrypt a test token |
| Change keychain fallback behavior | `token_storage.py` (`_get_encryption_key`) | Manual: test with keyring unavailable |
| Change Metabase admin-password storage | `metabase_credentials.py` | Unit: `pytest tests/unit/test_metabase_credential_store.py -q` |
| Change Metabase metadata/credential migration boundary | `metabase_config.py` | Unit: `pytest tests/unit/test_metabase_config.py -q` |
| Implement key rotation | `token_storage.py` (`rotate_encryption_key`) | Manual: verify tokens re-encrypted with new key |

## Dependencies

**Imports from:**
- `keyring` — OS keychain access for master encryption key
- `cryptography.fernet` — Fernet symmetric encryption
- `rich` — console output for warnings/status

`metabase_credentials.py` uses `keyring` where available and an owner-only
fallback outside the project directory. It must not be redirected to a
project-local file, sync path, or backup archive.

Both stores keep a keychain copy and a fallback file, and the keychain can answer
differently between runs (allow, deny, locked). `SecureTokenStorage` therefore
decrypts with every key it can find (keychain, then `.dlt/.encryption_key`),
never creates a key while decrypting, and reuses an existing file key before
generating one. `MetabaseCredentialStore.load()` reads the fallback file when
the keychain has no entry, a keychain save removes a stale fallback copy once
the keychain reads back the same value, and `delete()` cleans both places.
Tests: `tests/unit/test_keychain_*_continuity.py` (scripted keyring in
`tests/factories/scripted_keyring.py`).

`metabase_config.py` is the only access boundary for project-local Metabase
metadata and administrator credentials. It preserves a read-only legacy YAML
password fallback only while lifecycle migration establishes protected storage;
callers must not write `admin.password` to `.dango/metabase.yml`.

**Used by:**
- No production caller imports the Metabase configuration boundary yet; caller
  migration will allow both `dango.auth` and `dango.visualization` to depend on
  this lower-level package.
- OAuth token encryption remains isolated in `SecureTokenStorage`.

## Testing

- **Unit:** `tests/unit/test_metabase_credential_store.py` covers keyring and
  fallback behavior, file permissions, malformed state, and project-id guards.
- **Unit:** `tests/unit/test_metabase_config.py` covers metadata validation,
  protected credential preference, legacy compatibility, and atomic metadata writes.
- **Integration:** None yet
- **Manual:** Instantiate `SecureTokenStorage(project_root)`, call `encrypt_token({"key": "value"})`, then `decrypt_token()` on the result

## Don't Modify

| File | Reason |
|------|--------|
| `token_storage.py` `SERVICE_NAME` / `KEY_NAME` constants | Changing these orphans existing keys stored in user OS keychains |
| `token_storage.py` Fernet encryption format | Existing encrypted tokens would become unreadable |
