# TESTING.md - manual test plan

This file is the **manual** checklist: the things that need a real browser, a real Telegram account or a real
process on a real machine. Everything that can be checked by a program lives in the automated suite instead -
see [`tests/README.md`](tests/README.md) (`pip install -r requirements-dev.txt && python -m pytest`).

Automated and manual coverage overlap on purpose in sections B and C (the HTTP cases below are all asserted by
`tests/api/`), so you can skip them after a normal change and keep them as a smoke test against a deployed instance.

**Not covered by anything automated, so do these by hand after a relevant change:**
the real Telegram login (code / 2FA), a live archiver actually seeing a deletion, section D (the browser UI),
and the Windows toggle scripts.

---

## A. Prerequisites

1. Postgres running, control schema migrated (archive databases are created per account later, by the app itself):
   ```bash
   alembic -c alembic_control.ini upgrade head
   ```
2. `.env` has `CONTROL_DATABASE_URL`, `FERNET_KEY`, `JWT_SECRET` (and, for the first-account flow, `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN`).
   There are no `TG_*`, `DATABASE_URL`, `DB_PATH`, `SESSION_NAME` or `OWNER_USER_ID` settings any more - remove them if they're still in your `.env` from an earlier version.
3. Start the API: `uvicorn api.server:app --host 127.0.0.1 --port 8000`
4. Create the first (admin) account in the web UI by registering with the bootstrap token as the invite code -
   or from a shell: `python scripts/manage_admin.py create --username youruser`.
   Then follow Settings -> Telegram setup (credentials, login code, "Create archive database").
   Manual alternative for the archive: create a database yourself, migrate it with
   `alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head`, then
   `python scripts/manage_admin.py set-archive --username youruser --db-name DBNAME`.

---

## B. `scripts/manage_admin.py`

Subcommands: `create`, `create-invite`, `set-archive`, `delete-admin` (there is no `promote`: the database allows exactly one admin).
Argument and confirmation behaviour is unit-tested; the interactive prompts are not.

| Test | Command | Expected outcome |
|---|---|---|
| Create | `create --username alice` | Prompts for password (hidden input) twice; on match, creates the admin, prints `Created admin user 'alice' (id=N).` Confirm in DB: `is_admin=true`, `archive_db_ref`/`telegram_*` columns still NULL. |
| Second admin | `create --username bob` | Refused: an admin already exists. Nothing written. |
| Duplicate username | `create --username alice` again | Clean error before any prompt, exit code 1, nothing written. |
| Invite | `create-invite --created-by alice --expires-hours 48` | Prints a single-use token once. Registering with it works; reusing it gives the generic "invalid, expired, or already used" error. |
| Set archive | `set-archive --username alice --db-name alice_db` | Shows the target account and a reminder that this doesn't create/migrate the database; `y` sets `archive_db_ref='alice_db'`. |
| Set archive overwrite | `set-archive --username alice --db-name other_db` | Explicitly warns `This OVERWRITES the existing value` before asking to confirm. |
| Delete admin | `delete-admin --username alice` | Irreversible; you must retype the username. Removes the account, its archive database and its Telegram credentials. |
| Cancel | Any of the above, answer anything but `y` | `Cancelled. Nothing was written.` - confirm no DB row actually changed. |

---

## C. HTTP API (curl)

### C1. Registration

Needs an invite: create one in the web UI (admin -> Invites), with `scripts/manage_admin.py create-invite`, or use
`TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` on a fresh instance with no admin.

| Test | Expected outcome |
|---|---|
| `POST /auth/register` with valid invite+username+password | **201**. Body is `{"access_token": "...", "token_type": "bearer"}` - **no `refresh_token` field**. Response has a `Set-Cookie: televault_refresh=...` header with `HttpOnly`, `Path=/api/auth`, `SameSite=Strict`. |
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
| **Regression test - this is the one that matters:** while account A is throttled, log into a **different, real** account B from the same IP | **200** - B is completely unaffected. If B also gets blocked, the per-account throttle has regressed back to per-IP. |
| 5 failed logins against **usernames that don't exist**, then a 6th nonexistent-username attempt, same IP | **429** `"...from this address."` A real account logging in from that same IP right after should still succeed - this bucket is separate from C2's per-account one. |

### C3. Refresh (use `curl -c/-b cookiejar.txt`, no request body needed)

| Test | Expected outcome |
|---|---|
| Valid cookie | **200**, new `access_token`, `Set-Cookie` rotates to a **different** token value. |
| No cookie sent | **401** `"No refresh session found. Please log in again."` |
| Present an **already-rotated** (stale) cookie | **401** `"...already used. All sessions...have been logged out as a precaution..."` - then confirm a sibling session for the same account (if one existed) is **also** now dead. |
| Present a cookie from a session that was **explicitly logged out** | **401** `"You have been logged out. Please log in again."` - the plain message, NOT the "precaution" one - and confirm a **different, still-active** session for the same account is **unaffected**. |
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
| Account with `archive_db_ref = NULL` | Same endpoints → **409** `"Your archive hasn't been set up yet..."` - **not** 403. |
| `archive_db_ref` pointing at a database that doesn't actually exist | **503** `"...currently unavailable..."` |

### C7. Per-account workers - `telethon.py`/`backfill.py`

| Test | Expected outcome |
|---|---|
| Fresh account, Telegram not linked: `POST /api/telethon/start`, `POST /api/backfill/start` | **409** reason `telegram_not_linked` (not 403, and nothing is spawned). |
| Telegram linked, no archive yet | **409** reason `archive_unattached`. |
| `GET /api/telethon/status`, `/api/backfill/status` for any logged-in account | **200**, and each account sees only its **own** state (start an archiver for account A; account B still reports `running: false`). |
| Fully set up account: `POST /api/telethon/start` | **200** `{"started": true}`; a worker runs as `main.py --user-id <id>` - the Telegram session is **not** on its command line (check `ps`). Heartbeat appears in `data/users/<id>/`. |
| Same account: start again / start a backfill while the archiver runs | **409** `already_running` / `archiver_connected`. |
| Account A's archiver running, account B starts theirs | **Both** run - one account's worker never blocks another's. |
| Saved Telegram session revoked (Telegram -> Devices) then start | Worker exits within seconds with a clear log line; status never becomes `running`. A backfill in the same situation leaves a `backfill_runs` row with status `error`. |
| Admin deletes or unlinks account B while only account A's archiver runs | Allowed - only the **target's** own processes block it. |
| Wrong `FERNET_KEY` / unknown id: `python main.py --user-id N` | One-line error (`Could not decrypt...` / `No account with id=N.`), exit code 1, no traceback. |

---

## D. Browser

| Test | Expected outcome |
|---|---|
| Visit `/login.html` logged out | Form shows immediately, no redirect. |
| Submit correct credentials | Redirects to `/index.html`; Chats view loads (empty is fine with no archived messages); logout button and archiver status visible. |
| Visit `/login.html` or `/register.html` **while already logged in** | Immediately redirects to `/index.html` - the form should never flash. |
| Visit `/index.html` fresh, no prior session (incognito) | Brief loading state, then redirect to `/login.html` - the app shell itself should never flash visible first. |
| DevTools → Application → Cookies | `televault_refresh` shows `HttpOnly` ✓, `Path=/api/auth`, `SameSite=Strict`. Typing `document.cookie` in the console must **not** show it. |
| Click "Log out" | Redirects to `/login.html`; pressing Back afterward bounces straight back to `/login.html` (doesn't show stale app content). |
| Register with an invite token | Auto-redirects to `/index.html` on success (no separate login step needed). |
| Hard-reload (`Ctrl+Shift+R`) with the OS or saved theme set to dark, then to light | The theme icon matches the theme immediately (moon in dark, sun in light) - before any click. |
| Settings -> Telegram setup, enter api_id + api_hash and submit | Moves on to the phone-number step. A wrong pair is only reported at the next step (send code), not here. |
| Register with a username like `a b` or `ab`, or a 129-character password | The browser refuses the field before sending; if the request does go out, a generic "values are not valid" message appears (never a bare "Something went wrong"). |
| Register with mismatched password/confirm fields | Inline "Passwords don't match" - no request sent. |
| Open two tabs, log in on one, then load `/index.html` in the other | Second tab silently refreshes using the shared cookie and loads normally - expected, not a bug (access token is per-tab in memory; the cookie is shared). |
