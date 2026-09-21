# TESTING.md — Auth / multi-user feature

Manual test plan for everything built so far: cookie-based auth, per-account login throttling,
refresh-token rotation/reuse-detection, per-user archive routing, and the standalone
login/register pages.

**What's actually been run already:** every backend case below (sections A–C) was executed against
a real local Postgres during development, not just written. Section D (browser behavior) has
 been run and tested in a real browser.

---

## A. Prerequisites

1. Postgres running, both schemas migrated:
   ```bash
   alembic upgrade head
   alembic -c alembic_control.ini upgrade head
   ```
2. `.env` has `FERNET_KEY`, `JWT_SECRET`, `DATABASE_URL`, `CONTROL_DATABASE_URL` set.
   `OWNER_USER_ID` no longer exists — remove it if it's still in your `.env` from an earlier version.
3. Bootstrap your own account and point it at your real archive:
   ```bash
   python scripts/manage_admin.py create --username youruser
   python scripts/manage_admin.py set-archive --username youruser --db-name <your DATABASE_URL's db name>
   ```
4. Start the API: `uvicorn api.server:app --host 127.0.0.1 --port 8000`

---

## B. `scripts/manage_admin.py`

| Test | Command | Expected outcome |
|---|---|---|
| Create | `create --username alice` | Prompts for password (hidden input) twice; on match, creates the user, prints `Created admin user 'alice' (id=N).` Confirm in DB: `is_admin=true`, `archive_db_ref`/`telegram_*` columns still NULL. |
| Duplicate create | `create --username alice` again | Clean error before any prompt: `A user named 'alice' already exists...`, exit code 1, nothing written. |
| Promote | `promote --username alice` (or `--id N`) | Shows target account, asks `Proceed? [y/N]`; `y` flips `is_admin=true` and logs an `admin_promoted_via_script` audit row. |
| Promote idempotency | `promote --username alice` again | `'alice' (id=N) is already an admin. Nothing to do.` — **no** new audit row written (check `auth_audit_log`). |
| Promote missing user | `promote --id 9999` | `No user found with id=9999. Nothing was written.`, exit code 1. |
| Set archive | `set-archive --username alice --db-name alice_db` | Shows the target account and a reminder that this doesn't create/migrate the database; `y` sets `archive_db_ref='alice_db'`. |
| Set archive overwrite | `set-archive --username alice --db-name other_db` | Explicitly warns `This OVERWRITES the existing value: 'alice_db'.` before asking to confirm. |
| Cancel | Any of the above, answer anything but `y` | `Cancelled. Nothing was written.` — confirm no DB row actually changed. |

---

## C. HTTP API (curl)

### C1. Registration

Requires an invite row (no admin UI for this yet — insert by hand):
```sql
INSERT INTO invites (token, created_by, expires_at) VALUES ('test-invite', 1, now() + interval '1 day');
```

| Test | Expected outcome |
|---|---|
| `POST /auth/register` with valid invite+username+password | **201**. Body is `{"access_token": "...", "token_type": "bearer"}` — **no `refresh_token` field**. Response has a `Set-Cookie: televault_refresh=...` header with `HttpOnly`, `Path=/api/auth`, `SameSite=Strict`. |
| Same invite token again | **400** `"That invite token is invalid, expired, or already used."` |
| Nonexistent invite token | **400**, same message (invites don't leak whether they ever existed). |
| Username already taken | **409** `"That username is already taken."` |

### C2. Login

| Test | Expected outcome |
|---|---|
| Correct username/password | **200**, same body/cookie shape as register. |
| Wrong password | **401** `"Incorrect username or password."` |
| Nonexistent username | **401**, **identical message** to wrong-password (confirms no username enumeration). |
| Locked account (`UPDATE users SET is_locked=true`) | **403** `"This account is locked. Contact an administrator."` |
| 5 wrong passwords against **one** account, then a 6th attempt (even with the correct password) | **429** `"Too many failed login attempts for this account."` |
| **Regression test — this is the one that matters:** while account A is throttled, log into a **different, real** account B from the same IP | **200** — B is completely unaffected. If B also gets blocked, the per-account throttle has regressed back to per-IP. |
| 5 failed logins against **usernames that don't exist**, then a 6th nonexistent-username attempt, same IP | **429** `"...from this address."` A real account logging in from that same IP right after should still succeed — this bucket is separate from C2's per-account one. |

### C3. Refresh (use `curl -c/-b cookiejar.txt`, no request body needed)

| Test | Expected outcome |
|---|---|
| Valid cookie | **200**, new `access_token`, `Set-Cookie` rotates to a **different** token value. |
| No cookie sent | **401** `"No refresh session found. Please log in again."` |
| Present an **already-rotated** (stale) cookie | **401** `"...already used. All sessions...have been logged out as a precaution..."` — then confirm a sibling session for the same account (if one existed) is **also** now dead. |
| Present a cookie from a session that was **explicitly logged out** | **401** `"You have been logged out. Please log in again."` — the plain message, NOT the "precaution" one — and confirm a **different, still-active** session for the same account is **unaffected**. |
| Present a cookie belonging to a now-locked account | **403** `"This account is locked..."`, and all of that account's other sessions are revoked too. |

### C4. Logout

| Test | Expected outcome |
|---|---|
| Valid cookie | **200** `{"logged_out": true}`, `Set-Cookie` clears it (`Max-Age=0`). |
| No cookie / garbage cookie | Still **200** `{"logged_out": true}` (no-op, not an error). |

### C5. `GET /auth/me`

| Test | Expected outcome |
|---|---|
| Valid access token | **200** with `id`, `username`, `is_admin`, `created_at`, `last_login_at`. Confirm `password_hash` and `telegram_*` columns are **absent**. |
| No/garbage token | **401**. |

### C6. Archive access (`get_archive_connection`)

| Test | Expected outcome |
|---|---|
| Account with `archive_db_ref` set to a real, migrated database | `GET /api/chats` / `/messages` / `/deleted` / `/stats` → **200**. |
| Account with `archive_db_ref = NULL` | Same endpoints → **409** `"Your archive hasn't been set up yet..."` — **not** 403. |
| `archive_db_ref` pointing at a database that doesn't actually exist | **503** `"...currently unavailable..."` |

### C7. Instance control (`require_instance_owner`) — `telethon.py`/`backfill.py`

| Test | Expected outcome |
|---|---|
| The one account whose `archive_db_ref` matches this instance's own `DATABASE_URL` | `GET /api/telethon/status`, `/api/backfill/status` → **200**. |
| Any other account — **including an admin** | **403** `"This account does not control this instance's Telegram connection."` This is the key check: promoting someone to admin must never grant them this. |

---

## D. Browser

| Test | Expected outcome |
|---|---|
| Visit `/login.html` logged out | Form shows immediately, no redirect. |
| Submit correct credentials | Redirects to `/index.html`; Chats view loads (empty is fine with no archived messages); logout button and archiver status visible. |
| Visit `/login.html` or `/register.html` **while already logged in** | Immediately redirects to `/index.html` — the form should never flash. |
| Visit `/index.html` fresh, no prior session (incognito) | Brief loading state, then redirect to `/login.html` — the app shell itself should never flash visible first. |
| DevTools → Application → Cookies | `televault_refresh` shows `HttpOnly` ✓, `Path=/api/auth`, `SameSite=Strict`. Typing `document.cookie` in the console must **not** show it. |
| Click "Log out" | Redirects to `/login.html`; pressing Back afterward bounces straight back to `/login.html` (doesn't show stale app content). |
| Register with a hand-inserted invite token | Auto-redirects to `/index.html` on success (no separate login step needed). |
| Register with mismatched password/confirm fields | Inline "Passwords don't match" — no request sent. |
| Open two tabs, log in on one, then load `/index.html` in the other | Second tab silently refreshes using the shared cookie and loads normally — expected, not a bug (access token is per-tab in memory; the cookie is shared). |
