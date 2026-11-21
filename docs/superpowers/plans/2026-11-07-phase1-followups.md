# Phase 1 follow-ups (deferred review items)

Collected from per-task and whole-branch reviews of Phase 1. None blocks
Phase 2; fold each into the phase that touches the code.

## Before inviting the first teammate
- Revoke pending invites (admin "Revoke" button); deactivating an admin should cancel their invites.
- `SALESFINDER_PUBLIC_URL` for invite links (links built from `request.base_url` show `localhost` when the admin browses on the Mac).
- Doctor/settings warning when LAN mode is on but `allowed_hosts` lists only loopback names.

## Phase 2 (first new pages, first new migration)
- Add an upgrade → downgrade → upgrade migration round-trip test with migration 0002.
- `check_csrf` should accept an `X-CSRF-Token` header (HTMX actions outside forms); `LoginRequired` should return `HX-Redirect` for `HX-Request`.
- Load `AppConfig` once into `app.state`.
- Only update `last_seen_at` when older than ~60s (avoid a write per request/poll).
- Split templating/cookie helpers out of `web/deps.py`.
- PRG after `POST /admin/invites` (refresh currently creates a second invite).
- `error.html` should keep the nav for signed-in users.
- Merged-config validation errors can't say which YAML file holds the bad key.

## Hardening (can wait)
- `LoginRateLimiter` key growth is unbounded (add sweep/cap); rate-limited attempts write unbounded audit rows.
- Concurrent accept of two invites for the same email → `IntegrityError` 500 (map to `InviteError("exists")`).
- Admin invite route maps every `InviteError` to "exists".
- `_check_claude` can leave a child process if `communicate()` raises a non-timeout error; `proc.kill()` race.
- `--reload` child process never calls `configure_logging`.
- CLI `except (ValueError, LookupError)` is broad (also catches KeyError/IndexError).
- CI: add `permissions: contents: read` and a `concurrency` block.
- Bump `.pre-commit-config.yaml` ruff rev to match the locked ruff.

## Test gaps (can wait)
- Unquoted-time YAML guard, `localhost`/`::1` TLS exemption, nested local override.
- `test_audit_record` reads the identity-map object (use `populate_existing`).
- `test_deactivation_revokes_sessions` can't distinguish who revoked.
- Dummy-verify path in `authenticate`; invite accept without `password_confirm`.
- Doctor: `--help` timeout, migrations-not-at-head, no-shebang detail; `db upgrade`/`web`/`configure_logging`; alembic env `.env` fallback.

## Docs
- README: map mkcert output files to `SALESFINDER_TLS_CERTFILE`/`KEYFILE`; teammates need the mkcert root CA; `make setup` needs `createdb` rights.
