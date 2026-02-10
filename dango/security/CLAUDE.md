# security/

## Purpose

Encrypts OAuth tokens using Fernet symmetric encryption with the master key stored in the OS keychain (fallback: file-based key).

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Public exports | `SecureTokenStorage` |
| `token_storage.py` | Token encryption/decryption with OS keychain key storage | `SecureTokenStorage` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Change encryption approach | `token_storage.py` | Manual: encrypt then decrypt a test token |
| Change keychain fallback behavior | `token_storage.py` (`_get_encryption_key`) | Manual: test with keyring unavailable |
| Implement key rotation | `token_storage.py` (`rotate_encryption_key`) | Manual: verify tokens re-encrypted with new key |

## Dependencies

**Imports from:**
- `keyring` — OS keychain access for master encryption key
- `cryptography.fernet` — Fernet symmetric encryption
- `rich` — console output for warnings/status

**Used by:**
- No modules currently import this (prepared utility for future OAuth token encryption)

## Testing

- **Unit:** None yet (will be `tests/unit/test_token_storage.py`)
- **Integration:** None yet
- **Manual:** Instantiate `SecureTokenStorage(project_root)`, call `encrypt_token({"key": "value"})`, then `decrypt_token()` on the result

## Don't Modify

| File | Reason |
|------|--------|
| `token_storage.py` `SERVICE_NAME` / `KEY_NAME` constants | Changing these orphans existing keys stored in user OS keychains |
| `token_storage.py` Fernet encryption format | Existing encrypted tokens would become unreadable |
