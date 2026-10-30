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
| Account with `archive_db_ref = NULL` | Same endpoints → **409** `"Your archive database hasn't been created yet..."` (reason `archive_unattached`) - **not** 403. The message must not mention linking Telegram (that is a separate step). |
| `archive_db_ref` pointing at a database that doesn't actually exist | **503** `"...currently unavailable..."` |

### C6b. Chat view backend (`/api/chats/{id}/window`, `DELETE /api/chats/{id}`)

Needs a Bearer token of an account with an archive that holds a chat. Use `/docs` (Swagger) if curl is awkward.

| Test | Expected outcome |
|---|---|
| `GET /api/chats/<id>/window?limit=5` | **200**, the 5 newest messages **oldest first**, `has_older: true`, `has_newer: false`, each row has `edit_count`. |
| Take the first row's `id`, call again with `before=<that id>` | The 5 messages just before it, no overlap, no gap. Repeat until `has_older` is `false`. |
| `...window?around=<id of any message>&limit=7` | That message in the middle, `anchor_id` = its id. |
| `...window?around_date=2025-06-01` (a day inside your history) | Window around the first message on or after that day. |
| `...window?around=1&before=2` | **422**, reason `too_many_anchors`. `limit=0` or `limit=500` → **422**. |
| Window with a message id that belongs to ANOTHER chat | **404** `message_not_found`. |
| `DELETE /api/chats/<id>` on a chat you can spare, archiver running | **200** with counts; the chat is gone from `/api/chats`, `/api/chats/options` and the Stats table. Messages from it no longer appear in the feed. |
| Same chat, then someone writes to you again (or send yourself a message in that chat) | The chat reappears with only the new message. |
| `DELETE` on a chat that is on "Chats not archived" | **200**, `block_rule_kept: true`; the Settings list still shows it. |
| `DELETE` while a backfill is running | **409** `backfill_running`, nothing deleted. |
| `DELETE` on an id that does not exist | **404** `chat_not_found`. |

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
| Open the health panel on an account with nothing set up | A red **No** badge in front of "Telegram session found" (and of the database row, if that check fails); rows that pass carry a green **✓**. Switch the language to Ukrainian: the badges read **Ні** / the ✓ is announced as **Так**. |
| Telegram linked, no archive database yet: click the archiver button, then open Backfill and start one | Both alerts say "Your archive database hasn't been created yet." with a **Create archive database** link to the setup page (Ukrainian: "Базу даних вашого архіву ще не створено." / "Створити базу даних архіву"). Chats/Messages/Deleted/Stats/Health show the same text with the same link. Nothing mentions linking Telegram. |
| Setup page with Telegram linked and the archive created | The green "all set" banner has a gap below it, the same size as the gap between the two cards under it. |
| Setup page, link step | The filled **Link with a QR code** button comes first, then an "or" line, then the phone-number form with a plain (not filled) **Send code** button. A failed attempt shows its error at the top of the step. |
| Look at the archiver button in the nav, running vs stopped, in both themes | Running: a filled **green** dot. Stopped: a **hollow grey ring**. They are easy to tell apart at a glance in light and dark. |
| With the archiver running, send yourself a message from another device, then switch from Chats to another tab and back (repeat for Messages, Stats, Health) | The new message / updated counts appear **without** a page reload and without a "Loading..." flash. In Deleted: expand a row, switch away and back - it stays expanded unless a new deletion arrived. Not covered by automated tests (no browser in the test setup). |
| Register with a username like `a b`, browser language and app language both Ukrainian | The whole rejection message is Ukrainian, including the 3-32 characters hint. |
| Settings -> Telegram setup, "Link with a QR code" | A QR picture appears and changes about every 30 s. In Telegram on your phone: Settings -> Devices -> Link Desktop Device, scan it. Within a couple of seconds the card shows "linked" (or asks for your two-step password first). Back cancels. Not covered by automated tests: the real scan. |
| Telegram linked: click "Relink a different account" | Opens the **QR** step (not the API id/hash form), with a note that the old Telegram session is not ended, and a **Cancel - keep the current link** button. Cancel returns to "Linked" and nothing changes (the archiver keeps working). "Use different credentials" opens the form with a warning that saving signs the account out; Cancel is there too. |
| Telegram linked: "Unlink Telegram" | The confirm text says TeleVault also ends its own session in Telegram. After confirming, the entry for TeleVault is gone from Telegram's Settings -> Devices. With the server offline from Telegram (or the network cut), unlinking still works and a note says to remove the entry in Telegram yourself. **Real Telegram only - not covered by automated tests.** |
| Settings -> **Chats not archived**: pick a chat, "Stop archiving" | The chat moves from the picker into the list below with its name and the date; the picker and the empty-state text update. "Archive again" puts it back in the picker. Switch the language to Ukrainian: every string in the card changes without reloading the list. With no archive database yet, the card shows the "hasn't been created yet" text with a link to the setup page. |
| With the archiver running, block a chat, then send a message **and edit it** from another device in that chat; send one in a different chat too | The blocked chat's message and edit never appear in Messages/Deleted/Stats (no restart needed); the other chat's message does. Messages archived **before** the block are still there. Delete one of those old messages in the blocked chat: it is still flagged as deleted (deletions are intentionally not filtered). **Real Telegram only.** |
| Unblock that chat and send another message | It is archived again; what was sent while it was blocked stays absent. **Real Telegram only.** |
| Block a small group, then upgrade it to a supergroup in Telegram, and write in it | The new supergroup stays blocked and the list shows the chat once, not twice. **Real Telegram only.** |
| Start a backfill (all chats) with one chat blocked; then one for that chat alone (`--chat`) | The blocked chat is skipped with a log line "on your ingest blocklist" in both cases and nothing of it is stored; the first run's summary line says how many chats were skipped. **Real Telegram only.** |
| On an archive created before this release, before running `python scripts/migrate_archives.py` | The archiver keeps archiving everything and logs one warning about the missing table; Settings -> Chats not archived says the archive needs updating. After the migration the card works without restarting anything. |
| On that old archive: `python scripts/migrate_archives.py --dry-run`, then without `--dry-run`, then `--dry-run` again | First run lists it as `OUTDATED` and changes nothing; second prints `migrated`; third shows `ok`. No password or URL appears anywhere. Reload Settings: the card works, no restart needed. |
| On an archive created before this release: `python scripts/secure_archives.py --dry-run`, then without `--dry-run`, then again | `SHARED` for it first (nothing changed), then `secured`, then `ok ... has its own login`. No password appears in any output. Afterwards restart the archiver: it keeps archiving; chats, messages and the blocklist card still load. |
| In `psql` as the server login: `\du televault_archive_*` and `SELECT datname, datacl FROM pg_database;` | One role per archive, none with Superuser / Create DB / Create role; the archive databases' ACL lists only their own role (no `=c/...` PUBLIC entry), and the control database has none either. |
| Create a new account's archive in the web UI, then delete that account (admin panel) | The archive works right after creation; after deletion both the database and its role are gone from `\l` / `\du`. |
| Settings -> **Telegram devices** with the archiver and backfill stopped: "Show my devices" | The list matches Telegram's own Settings -> Devices (name, app, system, place, IP, dates). TeleVault's own entry has a badge and no button. Opening the Settings tab alone sends no request. **Real Telegram only.** |
| Same card: "End session" on another device, then "End all other devices" | Each asks for confirmation; afterwards those devices are signed out in Telegram (check on the phone) and the list refreshes. If TeleVault's own session is younger than about a day, Telegram refuses and the card says so (this rule comes from Telegram's documentation; confirm it on your account). **Real Telegram only.** |
| Same card with the archiver running | A clear "stop the archiver (and any backfill) first" message; nothing connects to Telegram. |
| Settings -> Telegram setup, request a login code for your own number | The code step explains where Telegram put it (typically: a message in the chat named "Telegram" inside an app you are logged in to, not an SMS), in the app language. The server log has a matching `delivery=` line. |
| Settings -> Telegram setup, enter api_id + api_hash and submit | Moves on to the phone-number step. A wrong pair is only reported at the next step (send code), not here. |
| Register with a username like `a b` or `ab`, or a 129-character password | The browser refuses the field before sending; if the request does go out, a generic "values are not valid" message appears (never a bare "Something went wrong"). |
| Register with mismatched password/confirm fields | Inline "Passwords don't match" - no request sent. |
| Open two tabs, log in on one, then load `/index.html` in the other | Second tab silently refreshes using the shared cookie and loads normally - expected, not a bug (access token is per-tab in memory; the cookie is shared). |