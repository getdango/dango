# auth/

## Purpose

User authentication and access control for Dango. Handles password-based login with optional TOTP 2FA, OAuth social login (Google + GitHub), session management (cookie + API key), role-based access control (Admin/Editor/Viewer with 29 named permissions), invite-based user onboarding, Metabase SSO session bridging, brute-force lockout, and audit logging.

## Files

| File | Lines | Purpose | Key Exports |
|------|-------|---------|-------------|
| `__init__.py` | 238 | Re-exports 95 public symbols | All public API |
| `models.py` | 172 | Pydantic models | `Role`, `User`, `UserCreate`, `UserUpdate`, `UserResponse`, `Session`, `APIKey` |
| `database.py` | 529 | SQLite CRUD (WAL mode, FK enforcement) | `create_user()`, `get_user_by_*()`, `list_users()`, `update_user()`, `create_session()`, `get_session_by_token()`, `create_api_key()` |
| `security.py` | 375 | Pure crypto utilities | `hash_password()`, `verify_password()`, `check_password_strength()`, `generate_session_token()`, `generate_api_key()`, `generate_invite_token()`, `generate_recovery_codes()` |
| `sessions.py` | 289 | High-level session + API key lifecycle | `create_session()`, `validate_session()`, `validate_partial_session()`, `create_api_key()`, `validate_api_key()` |
| `permissions.py` | 195 | 29 permissions, 3 role mappings | `PERMISSIONS`, `ROLE_PERMISSIONS`, `has_permission()`, `require_permission()` |
| `lockout.py` | 181 | Brute-force protection (5 attempts / 15-min) | `record_failed_login()`, `check_account_locked()`, `unlock_account()` |
| `audit.py` | 188 | 22 event types to `.dango/logs/audit.jsonl` | `AuditEvent`, `log_auth_event()`, `query_audit_log()` |
| `admin.py` | 124 | Bootstrap + path helpers | `ensure_admin()`, `is_auth_enabled()`, `get_auth_db_path()` |
| `totp.py` | 220 | TOTP 2FA: setup/verify/enable/disable, recovery codes | `generate_totp_secret()`, `verify_totp_code()`, `setup_totp()`, `enable_totp()`, `consume_recovery_code()` |
| `oauth_login.py` | 307 | OAuth provider ABC + Google/GitHub implementations | `OAuthLoginProvider`, `GoogleOAuthProvider`, `GitHubOAuthProvider`, `get_provider()` |
| `metabase_sync.py` | 498 | Sync users/roles to Metabase (encrypted passwords) | `sync_user_to_metabase()`, `sync_all_users_to_metabase()`, `sync_user_role()`, `decrypt_metabase_password()` |
| `metabase_bridge.py` | 152 | Async SSO session bridging on login/logout | `bridge_metabase_login()`, `bridge_metabase_logout()`, `ensure_metabase_synced()` |

## Architecture

### Two API Levels

- **`sessions.py`** (high-level): Validates timeouts, hashes tokens, orchestrates create/validate flows. Use this from web routes and CLI.
- **`database.py`** (low-level): Raw SQLite CRUD. Accepts pre-built objects. Only call directly when you need fine-grained control.

### Session Lifecycle

- **Full session:** 60-min idle timeout, 30-day absolute expiry. Cookie: `dango_session`, HttpOnly, SameSite=Lax.
- **Partial session:** 5-min timeout. Created when password is verified but 2FA is pending.
- **API key:** No expiry. `dango_ak_` prefix, SHA-256 hashed in DB. Bearer token auth.

### Key Conventions

- **Login returns 400** for bad credentials (not 401). The 401 status is reserved for "you need to authenticate" (missing/expired session). This prevents browsers from showing native auth dialogs.
- **Timing oracle prevention:** `verify_password("dummy", DUMMY_HASH)` called on all login failure paths to equalize bcrypt timing.
- **Lockout is identity-blind:** unknown and inactive emails get identical 400 responses (no user enumeration).
- **Metabase bridge:** `_bridge_metabase_session()` in `web/routes/auth.py` consolidates Metabase SSO cookie bridging. Called from 4 login paths (password, OAuth×2, 2FA verify).

### Password Login Flow

```
POST /api/auth/login
  │
  ├─ lockout.check_account_locked() ──── locked? → 423
  │
  ├─ database.get_user_by_email() ────── not found? → verify_password(dummy) → 400
  │
  ├─ security.verify_password() ──────── wrong? → lockout.record_failed_login() → 400
  │
  ├─ user.is_active? ────────────────── inactive? → 400
  │
  ├─ user.totp_enabled?
  │   ├─ YES → sessions.create_session(is_partial=True) → 200 {requires_2fa}
  │   │         └─ POST /api/auth/2fa/verify → validate_partial → full session
  │   └─ NO  → sessions.create_session() → full session
  │
  ├─ lockout.reset_failed_logins()
  ├─ metabase_bridge.bridge_metabase_login() → set metabase.SESSION cookie
  └─ 200 + Set-Cookie: dango_session
```

### Invite Flow

```
Admin: POST /api/users (with send_invite=true)
  │
  ├─ security.generate_invite_token() → (raw_token, token_hash)
  ├─ database.create_user() with invite_token_hash, invite_expires_at
  └─ Return invite URL: /invite/{raw_token}

User visits: GET /invite/{token}
  └─ Renders password-setup form

User submits: POST /api/auth/accept-invite
  │
  ├─ security.hash_token(token) → lookup user by invite_token_hash
  ├─ Validate: not expired, not already accepted
  ├─ security.hash_password(new_password)
  ├─ database.update_user() → set password_hash, clear invite fields
  ├─ sessions.create_session() → full session
  ├─ metabase_bridge.bridge_metabase_login()
  └─ 200 + Set-Cookie: dango_session
```

### OAuth Login Flow

```
GET /api/auth/oauth/{provider}/authorize
  └─ Redirect to provider (Google/GitHub) with state token

GET /api/auth/oauth/{provider}/callback
  │
  ├─ Exchange code for access token
  ├─ Fetch user info (email, name, provider ID)
  │
  ├─ database.get_user_by_oauth(provider, oauth_id)
  │   ├─ Found → use existing user
  │   └─ Not found → get_user_by_email()
  │       ├─ Found + no conflicting OAuth link → auto-link to existing user
  │       └─ Not found → redirect with error (admin must pre-create user)
  │
  ├─ sessions.create_session()
  ├─ metabase_bridge.bridge_metabase_login()
  └─ Redirect to / + Set-Cookie: dango_session
```

### Request Authentication (Middleware)

```
Every request → web/middleware/auth.py
  │
  ├─ Parse dango_session cookie OR Authorization: Bearer header
  │
  ├─ Cookie path:
  │   ├─ sessions.validate_session() → set request.state.user
  │   └─ Expired/invalid → clear cookie, continue as anonymous
  │
  ├─ Bearer path:
  │   ├─ Starts with "dango_ak_" → sessions.validate_api_key()
  │   └─ Other → sessions.validate_session() (session token in header)
  │
  ├─ CSRF check (non-GET with cookie auth):
  │   └─ Require X-Requested-With or X-CSRF-Protection header
  │
  └─ Route handler → require_permission("source.sync") enforces RBAC
```

## Permission Matrix

29 permissions across 9 domains. Admin has wildcard `*` (all permissions). Source: `permissions.py`.

| Domain | Permission | Admin | Editor | Viewer |
|--------|-----------|:-----:|:------:|:------:|
| **Source** | `source.view` | Y | Y | Y |
| | `source.view_credentials` | Y | | |
| | `source.sync` | Y | Y | |
| | `source.manage` | Y | Y | |
| **CSV** | `csv.upload` | Y | Y | |
| | `csv.delete` | Y | Y | |
| **dbt** | `dbt.view` | Y | Y | Y |
| | `dbt.run` | Y | Y | |
| | `dbt.manage` | Y | Y | |
| **Dashboard** | `dashboard.view` | Y | Y | Y |
| | `dashboard.create` | Y | Y | |
| | `query.execute` | Y | Y | |
| | `dashboard.manage` | Y | | |
| **Platform** | `health.view` | Y | Y | Y |
| | `logs.view` | Y | Y | Y |
| | `platform.manage` | Y | | |
| | `config.view` | Y | Y | |
| | `config.manage` | Y | | |
| **Auth** | `users.view` | Y | | |
| | `users.manage` | Y | | |
| | `auth.manage` | Y | | |
| | `audit.view` | Y | | |
| **Notebooks** | `notebooks.view` | Y | Y | Y |
| | `notebooks.execute` | Y | Y | |
| | `notebooks.manage` | Y | Y | |
| **Governance** | `governance.view` | Y | | Y |
| | `governance.manage` | Y | | |
| **Scheduler** | `scheduler.view` | Y | Y | Y |
| | `scheduler.manage` | Y | | |

## Metabase SSO

Session bridging syncs Dango auth state to Metabase so users get single sign-on.

**Login bridge** (`bridge_metabase_login`): Decrypt stored Metabase password (Fernet via `SecureTokenStorage`) → POST `/api/session` to Metabase → set `metabase.SESSION` cookie on response.

**Logout bridge** (`bridge_metabase_logout`): DELETE `/api/session` on Metabase → clear `metabase.SESSION` cookie.

**Proxy re-bridge** (`web/routes/metabase_proxy.py`): When proxy gets 401 from Metabase, re-bridges automatically using stored credentials.

**Role sync** (`sync_user_to_metabase`): Admin → Metabase superuser. Editor → "Dango Editors" group. Viewer → "All Users" only (default read access). Groups created by `ensure_metabase_groups()`.

**Troubleshooting:**
- Missing `metabase_user_id` on User → run `sync_user_to_metabase()` for that user
- Stale group membership → `sync_all_users_to_metabase()` for full reconciliation
- Missing `metabase.yml` → Metabase not configured, bridge is a no-op
- Lost encrypted password → re-generate via `generate_metabase_password()` + `encrypt_metabase_password()`

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new user | CLI: `dango auth add-user` (generates invite URL) | `pytest tests/unit/test_cli_auth.py` |
| Add a new OAuth provider | `oauth_login.py` (new provider class) + `web/routes/auth.py` (routes) | `pytest tests/unit/test_auth_oauth_login.py` |
| Add a new permission | `permissions.py` (`PERMISSIONS` set + `ROLE_PERMISSIONS` mapping) | `pytest tests/unit/test_auth_permissions.py` |
| Modify role permissions | `permissions.py` (`ROLE_PERMISSIONS` dict) | `pytest tests/unit/test_auth_permissions.py` |
| Add a new audit event | `audit.py` (`AuditEvent` enum) | `pytest tests/unit/test_auth_audit.py` |
| Debug session issues | `sessions.py` (`validate_session` / `validate_partial_session`) | `pytest tests/unit/test_auth_sessions.py` |
| Change password policy | `security.py` (`check_password_strength`) | `pytest tests/unit/test_auth_security.py` |
| Change lockout thresholds | `lockout.py` (constants at top of file) | `pytest tests/unit/test_auth_lockout.py` |

## Recovery Procedures

**Admin lockout (locked out of web UI):**
1. CLI: `dango auth unlock admin@example.com` (resets failed login counter)
2. Manual SQL: `sqlite3 .dango/auth.db "UPDATE users SET failed_login_attempts=0, locked_until=NULL WHERE email='...'"`
3. Nuclear: delete `.dango/auth.db` → `dango start` recreates schema + bootstraps admin

**auth.db rebuild (corrupted or lost database):**
1. Delete `.dango/auth.db`
2. Run `dango start` — recreates schema, bootstraps admin with random password
3. Re-add users via `dango auth add-user` (invite flow)
4. Run `sync_all_users_to_metabase()` to restore Metabase SSO

**Metabase reconciliation (users/roles out of sync):**
- Full sync: call `sync_all_users_to_metabase(db_path, project_root, metabase_url)`
- Single user: call `sync_user_to_metabase(db_path, user_id, project_root, metabase_url)`
- Password reset: `generate_metabase_password()` + `encrypt_metabase_password()` + update user

## Dependencies

**Imports from (dango modules):**
- `config.loader` — `ConfigLoader` (admin.py: reads project config for auth settings)
- `config.models` — `OAuthProviderConfig` (oauth_login.py: typed OAuth config)
- `exceptions` — `UserExistsError`, `UserNotFoundError`, `AuthenticationError`, `AuthorizationError`
- `logging` — `get_logger` (audit.py, lockout.py)
- `security.token_storage` — `SecureTokenStorage` (metabase_sync.py: Fernet encryption for Metabase passwords)

**External packages:** `pwdlib[bcrypt]`, `pyotp`, `httpx`, `requests`, `pydantic`, `rich`

**Used by:**
- `web/middleware/auth.py` (325 lines) — session/API key validation on every request
- `web/routes/auth.py` (~854 lines) — login, password change, OAuth, invite accept. Defines `_bridge_metabase_session()` helper (shared with auth_2fa.py).
- `web/routes/auth_2fa.py` (~328 lines) — TOTP setup/verify/disable. Imports `_bridge_metabase_session` from auth.py.
- `web/routes/users.py` (525 lines) — user CRUD, invite creation, reinvite
- `web/routes/ui.py` (183 lines) — login/account/invite page rendering
- `web/routes/metabase_proxy.py` (268 lines) — SSO re-bridging on 401
- `web/app.py` (301 lines) — first-run admin bootstrap in `startup_event()`
- `cli/commands/auth.py` (538 lines) — 12 auth subcommands (add-user, list-users, unlock, etc.)

## Testing

22 test files, 8061 lines total.

**Auth internals (13 files):**
```
pytest tests/unit/test_auth_models.py tests/unit/test_auth_database.py \
  tests/unit/test_auth_security.py tests/unit/test_auth_sessions.py \
  tests/unit/test_auth_permissions.py tests/unit/test_auth_lockout.py \
  tests/unit/test_auth_audit.py tests/unit/test_auth_admin.py \
  tests/unit/test_auth_totp.py tests/unit/test_auth_oauth_login.py \
  tests/unit/test_auth_api_keys.py tests/unit/test_metabase_sync.py \
  tests/unit/test_metabase_bridge.py
```

**Web + CLI integration (9 files):**
```
pytest tests/unit/test_web_auth.py tests/unit/test_web_auth_2fa.py \
  tests/unit/test_web_auth_invite.py tests/unit/test_web_auth_keys.py \
  tests/unit/test_web_auth_oauth.py tests/unit/test_web_users.py \
  tests/unit/test_auth_middleware.py tests/unit/test_auth_middleware_helpers.py \
  tests/unit/test_cli_auth.py
```

**Run all auth tests:**
```
pytest tests/unit/test_auth*.py tests/unit/test_metabase*.py \
  tests/unit/test_web_auth*.py tests/unit/test_web_users.py \
  tests/unit/test_cli_auth.py
```

**Manual verification:** `dango auth status`, `dango auth list-users`, `dango auth audit`

## Don't Modify

| Item | Reason |
|------|--------|
| `models.py` field names | All consumers (web routes, CLI, tests) depend on exact shapes |
| `security.py` `_API_KEY_PREFIX` (`dango_ak_`) | Existing API keys become unrecognizable |
| `security.py` bcrypt work factor | Invalidates all existing password hashes |
| `permissions.py` existing permission names | Web routes use string literals (`require_permission("source.sync")`) |
| `database.py` SQLite schema | Column changes need a migration (not yet automated for auth.db) |
| `audit.py` `AuditEvent` enum values | Audit log queries and filtering depend on exact strings |
| `oauth_login.py` `_PROVIDERS` keys | Must match `OAuthProviderConfig` provider names in config |
| `metabase_sync.py` group names (`Dango Editors`) | Changing orphans existing Metabase group memberships |
