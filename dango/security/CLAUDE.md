# security/

## Purpose

Encrypts OAuth tokens using Fernet symmetric encryption with the master key stored in the OS keychain (fallback: file-based key).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `SecureTokenStorage`, `MetabaseCredentialStore` |
| `metabase_credentials.py` | Per-project Metabase admin-password storage outside the project tree | `MetabaseCredentialStore` |
| `token_storage.py` | Token encryption/decryption with OS keychain key storage | `SecureTokenStorage` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change encryption approach | `token_storage.py` | Manual: encrypt then decrypt a test token |
| Change keychain fallback behavior | `token_storage.py` (`_get_encryption_key`) | Manual: test with keyring unavailable |
| Change Metabase admin-password storage | `metabase_credentials.py` | Unit: `pytest tests/unit/test_metabase_credential_store.py -q` |
| Implement key rotation | `token_storage.py` (`rotate_encryption_key`) | Manual: verify tokens re-encrypted with new key |

## Dependencies

**Imports from:**
- `keyring` — OS keychain access for master encryption key
- `cryptography.fernet` — Fernet symmetric encryption
- `rich` — console output for warnings/status

`metabase_credentials.py` uses `keyring` where available and an owner-only
fallback outside the project directory. It must not be redirected to a
project-local file, sync path, or backup archive.

**Used by:**
- No modules currently import this (prepared utility for future OAuth token encryption)

## Testing

- **Unit:** `tests/unit/test_metabase_credential_store.py` covers keyring and
  fallback behavior, file permissions, malformed state, and project-id guards.
- **Integration:** None yet
- **Manual:** Instantiate `SecureTokenStorage(project_root)`, call `encrypt_token({"key": "value"})`, then `decrypt_token()` on the result

## Don't Modify

| File | Reason |
|------|--------|
| `token_storage.py` `SERVICE_NAME` / `KEY_NAME` constants | Changing these orphans existing keys stored in user OS keychains |
| `token_storage.py` Fernet encryption format | Existing encrypted tokens would become unreadable |
