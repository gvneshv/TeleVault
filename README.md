# TeleVault

A personal Telegram userbot that archives all your messages in real time and
preserves deleted ones so you can retrieve them later - with a web UI to
browse, search, and review what's been archived.

**Phase 1 (userbot):** text messages only, all chat types, PostgreSQL storage.
**Phase 2 (web UI):** read-only REST API + installable PWA - Chats, Messages,
Deleted, Stats, and Health views, backfill for historical messages, a
per-view chat filter, EN/UK language support, and light/dark themes.

> **Status:** PostgreSQL is the storage layer (SQLAlchemy Core + Alembic
> migrations), replacing the SQLite-based storage from
> [`v1.2.0`](CHANGELOG.md#120--2026-09-07) and earlier. The migration
> itself shipped in [`v2.0.0`](CHANGELOG.md#200--2026-09-10); a round of
> reliability fixes found under real usage since then (a stopped Docker
> container, a deleted `data/` directory, Postgres disappearing mid-run),
> plus the chat filter and Backfill pagination, shipped in
> [`v2.1.0`](CHANGELOG.md#210--2026-09-13). See the CHANGELOG for the full
> writeup and what's next.

---

## Requirements

- Python 3.11 or newer
- Docker + Docker Compose (for the local Postgres instance - see step 2)
- A Telegram account
- Telegram API credentials (free - takes two minutes to get)

---

## 1. Get your Telegram API credentials

1. Go to **https://my.telegram.org** and log in with your phone number.
2. Click **"API development tools"**.
3. Fill in any app name and short name (e.g. `televault` / `tvault`) - these
   are just labels, they don't affect anything.
4. Copy your **App api_id** (a number) and **App api_hash** (a hex string).

> Keep these secret. Anyone with your api_id + api_hash can impersonate your
> app (though not your account without the login code).

---

## 2. Set up the project

```bash
# Clone the repo
git clone https://github.com/Gvneshv/TeleVault.git
cd TeleVault

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Start Postgres

```bash
docker compose up -d
```

This starts a local Postgres 16 instance in the background, matching the
credentials `.env.example`'s `DATABASE_URL` default already expects - no
extra configuration needed for local dev. Data persists in a Docker volume
across restarts (`docker compose down` stops the container without touching
your data; only `docker compose down -v` wipes it).

**Note for local development:** Postgres is a client-server database, unlike
SQLite - it needs to actually be running (this container up) any time you
want to connect to it at all, whether that's TeleVault itself, `psql`, or a
GUI client. `docker compose up -d` takes a couple of seconds and then just
runs quietly in the background; most people start it once at the beginning
of a dev session and leave it running. See `docker-compose.yml`'s comments
for more.

---

## 3. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```
TG_API_ID=12345678
TG_API_HASH=0123456789abcdef0123456789abcdef
TG_PHONE=+1234567890          # your number in international format
```

The other settings have sensible defaults - you can leave them as-is for
now. `DATABASE_URL` already matches the Postgres container started in step 2.

`FERNET_KEY`, `JWT_SECRET`, and `CONTROL_DATABASE_URL` are for the
auth/multi-user control database - see the next section for setting up
accounts once these are filled in.

---

## 4. Set up the database

With Postgres running (step 2), apply the archive schema:

```bash
alembic upgrade head
```

This creates all tables, indexes, and the `pg_trgm` extension used for
search. Safe to run again later after pulling new schema changes - Alembic
only applies migrations that haven't run yet.

Then apply the separate control-database schema (accounts/invites/refresh
tokens/audit log - kept apart from the archive above, see
`control_db/schema.py` for why):

```bash
alembic -c alembic_control.ini upgrade head
```

### Bootstrapping your own account

There's no self-registration yet - every account needs an invite, and every
invite needs an existing admin to have created it, so the very first account
(yours) has to be created directly:

```bash
python scripts/manage_admin.py create --username youruser
```

This prompts for a password (never pass one as a CLI argument - see the
script's own module docstring for why), creates an admin account bypassing
the invite requirement entirely, and prints the new account's id, e.g.
`Created admin user 'youruser' (id=1).` - no API server or login needed to
get this value, since the script talks to the control database directly.
It's the only supported way to create the first account; see
`control_db/schema.py`'s `invites` table for why that's a hard requirement
rather than an oversight. This account starts with no Telegram credentials
and no archive - both are separate steps below, same as for anyone else who
registers through an invite afterward.

### Inviting other people

Once at least one admin exists, generate invites from the command line
instead of writing SQL by hand:

```bash
python scripts/manage_admin.py create-invite --created-by youruser
```

Prints a single-use token (default 24h expiry, `--expires-hours` to change
it) - hand it to the invitee directly. They register with it at
`POST /auth/register`; the web UI's `register.html` calls this for you once
you have someone to send the link to.

### Linking a Telegram account

Registering (or being created via `manage_admin.py create`) only makes a
TeleVault account - it doesn't connect to Telegram yet. That's a separate,
per-account step, currently only reachable directly against the API (there's
no settings-page UI for this yet):

1. **Get your own Telegram API credentials** - the same `api_id`/`api_hash`
   pair from [my.telegram.org](https://my.telegram.org) as step 1 above,
   but this time it's *this account's own* pair, saved encrypted on its own
   row (not the `.env` values, which are still only used by `main.py`, the
   one instance-owner's live archiver - see `api/routes/telegram.py`'s
   module docstring for how the two relate):

   ```bash
   curl -X POST http://localhost:8000/api/telegram/credentials \
     -H "Authorization: Bearer <your access_token>" \
     -H "Content-Type: application/json" \
     -d '{"api_id": "12345", "api_hash": "your_api_hash"}'
   ```

2. **Request a login code**, sent by Telegram to the phone number itself
   (not stored anywhere - see `TelegramSendCodeIn`'s own docstring):

   ```bash
   curl -X POST http://localhost:8000/api/telegram/link/send-code \
     -H "Authorization: Bearer <your access_token>" \
     -H "Content-Type: application/json" \
     -d '{"phone": "+15551234567"}'
   ```

3. **Confirm the code** Telegram sent:

   ```bash
   curl -X POST http://localhost:8000/api/telegram/link/confirm \
     -H "Authorization: Bearer <your access_token>" \
     -H "Content-Type: application/json" \
     -d '{"code": "12345"}'
   ```

   If the account has Telegram's own two-factor password enabled, this
   comes back `{"linked": false, "needs_password": true}` instead of an
   error - call `/telegram/link/confirm` again with `{"password": "..."}`
   (not the code again) to finish.

Afterward, this account has an encrypted Telegram session saved, but still
no archive database to write to - that's still a separate, manual step
(see below) until automatic provisioning exists.

### Pointing an account at an archive database

Whether or not it's linked Telegram yet, an account needs `archive_db_ref`
set before it can use `/api/chats`, `/messages`, `/deleted`, or `/stats`
(they 409 with "hasn't been set up yet" otherwise). Create and migrate a
database for it the same way you did for `DATABASE_URL` in step 4 above,
then point the account at it:

```bash
python scripts/manage_admin.py set-archive --username youruser --db-name youruser_archive
```

This is a manual stand-in for real provisioning (automatically creating and
migrating a database as part of linking Telegram), which doesn't exist yet -
see that script's own module docstring.

### Migrating an existing SQLite archive

If you have an archive from a previous SQLite-based install
(`v1.2.0` or earlier) that you want to keep, migrate it now, before running
TeleVault against the new Postgres database for the first time:

```bash
python scripts/migrate_sqlite_to_postgres.py --dry-run   # preview counts, writes nothing
python scripts/migrate_sqlite_to_postgres.py             # the real migration
```

It reads from `DB_PATH` (read-only - your old file is never modified) and
writes into `DATABASE_URL`. Safe to interrupt and re-run if needed - see the
script's own module docstring for exactly how that safety works. It
verifies its own results (row counts, source vs. target) at the end and
exits with an error if anything doesn't match.

Starting fresh with no prior archive? Skip this - there's nothing to migrate.

---

## 5. First run

```bash
python main.py
```

**First-time only:** Telethon will prompt you for the verification code that
Telegram sends to your account (just like logging into a new device). Enter
it and press Enter. A `televault.session` file is created - this stores your
login so you won't be asked again.

You should see output like:

```
2026-05-12 18:00:00  INFO      utils.logging_setup    Logging initialised - level=INFO
2026-05-12 18:00:01  INFO      __main__               Starting TeleVault.
2026-05-12 18:00:02  INFO      __main__               Authenticated as: Alice (id=123456789)
2026-05-12 18:00:02  INFO      __main__               Event handlers registered.
2026-05-12 18:00:02  INFO      __main__               TeleVault is running. Press Ctrl-C to stop.
```

From this point, TeleVault is archiving every text message in real time.

---

## 6. Smoke test

With TeleVault running, open Telegram on your phone or desktop and:

1. **Send yourself a message** (open Saved Messages and type anything).
   You should see a log line:
   ```
   INFO  db.queries  Inserted message 1 from chat 123456789 -> internal id 1
   ```

2. **Delete that message.**
   You should see:
   ```
   INFO  db.queries  Flagged message 1 in chat 123456789 as deleted at ...
   ```

3. **Query the database directly** to confirm:
   ```bash
   docker compose exec postgres psql -U televault -d televault -c "
     SELECT text, is_deleted, deleted_at
     FROM messages
     ORDER BY archived_at DESC
     LIMIT 5;
   "
   ```
   (Or use any Postgres GUI client - DBeaver, pgAdmin, TablePlus, or a
   VSCode Postgres extension - pointed at `localhost:5432` with the
   credentials from `.env`.)

---

## 7. Stopping TeleVault

Press **Ctrl-C**. The shutdown is graceful - the database connection is
flushed and closed cleanly before the process exits.

---

## 8. Launch the web UI

The web UI is a separate process from the userbot - both can run at the same
time, reading/writing the same Postgres database (the API only ever reads,
via a read-only connection - see `api/dependencies.py`).

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` (rather than the default `127.0.0.1`) makes it reachable
from other devices on your network too (e.g. testing from a phone) - drop
it back to `127.0.0.1` if you only ever want this reachable from the
machine it's running on.

Then open **http://localhost:8000** in a browser - you'll land on the login
page. Register (you'll need an invite token - see "Inviting other people"
above) or log in with an account you already bootstrapped, and you should
see the Chats view load, with Messages, Deleted, Stats, Backfill, and
Health in the nav rail.

> **A freshly-registered account will show "hasn't been set up yet" on
> most tabs, and that's expected**, not a bug: those views need
> `archive_db_ref` set (see "Pointing an account at an archive database"
> above) before there's anything to show. Backfill and Telethon specifically
> only ever work for the one account that controls this instance's live
> userbot (see this README's Notes section on `require_instance_owner`) -
> every other account will see "This account does not control this
> instance's Telegram connection" there, which is expected too, not an
> error to fix.
>
> For exercising raw endpoints directly (without the web UI), **http://localhost:8000/api/docs**
> still works - click "Authorize" and paste the `access_token` from
> `POST /auth/login`.

A few things worth knowing:

- **Installable as an app:** most browsers will offer to install it (via the
  address bar or browser menu) since it ships a PWA manifest and service
  worker. Installed or not, it works the same.
- **Offline behaviour:** the app shell (HTML/CSS/JS) is cached for offline
  loading, but data always requires a live connection - `/api/*` is
  deliberately excluded from the cache, since this is private data and a
  stale cached result would be misleading, not just old.
- **Theme and language:** toggle at the bottom of the nav rail (☀/☾ for
  theme, EN/UK for language) - on narrow/mobile screens, where the nav
  collapses to a top bar, they move to the right end of that bar instead.
  Both persist across visits via `localStorage`.
- **Interactive API docs:** available at `http://localhost:8000/api/docs`
  (Swagger UI) if you want to explore the endpoints directly.
- **Deployment note:** for always-on use, run this the same way as the
  userbot (systemd, etc.), with Nginx proxying `/api/*` to this process and
  serving `/` - see `api/server.py`'s docstring for the exact setup.

---

## Project structure

```
televault/
├── alembic/                  # Schema migrations (Alembic) - source of truth is db/schema.py
│   ├── versions/
│   │   └── ..._baseline_schema.py
│   ├── env.py
│   └── script.py.mako
├── alembic.ini
├── api/                      # REST API (FastAPI) - read-only, serves web/ as static files
│   ├── routes/
│   │   ├── chats.py
│   │   ├── messages.py
│   │   ├── deleted.py
│   │   ├── stats.py
│   │   ├── health.py
│   │   ├── backfill.py
│   │   └── telethon.py       # archiver process status/start/stop, backed by the heartbeat file - read by the nav rail's toggle and the Backfill page
│   ├── schemas/               # Pydantic v2 response models
│   │   ├── chat.py
│   │   ├── message.py
│   │   ├── stats.py
│   │   └── common.py          # PaginatedResponse, HealthOut
│   ├── dependencies.py        # get_db() - read-only Postgres connection per request
│   ├── process_utils.py       # single-instance guard + heartbeat-file reading, shared by main.py and the telethon status route
│   └── server.py              # FastAPI app + static file mount
├── backfill.py                # Historical-message import - separate entry point from main.py; can't run at the same time as the live archiver (same Telegram session)
├── main.py                    # Userbot entry point
├── config.py                  # Settings loader (.env -> Settings dataclass)
├── db/
│   ├── connection.py          # Postgres connection pool (SQLAlchemy Engine + psycopg)
│   ├── schema.py               # Table definitions (SQLAlchemy Core) - read by Alembic, not applied at runtime
│   ├── queries.py               # All write operations (used by the userbot)
│   └── read_queries.py          # All read operations (used by the API)
├── scripts/
│   ├── migrate_sqlite_to_postgres.py  # One-time SQLite -> Postgres data migration
│   └── toggle_archiver.ps1 / .bat     # Windows shortcut to start/stop the live archiver
├── handlers/
│   ├── helpers.py       # Shared Telethon entity utilities
│   ├── on_message.py    # NewMessage handler
│   ├── on_delete.py     # MessageDeleted handler
│   └── on_edit.py       # MessageEdited handler
├── web/                  # Vanilla JS/HTML/CSS PWA - no build step
│   ├── css/
│   │   ├── base.css
│   │   └── variables.css
│   ├── js/
│   │   ├── i18n/
│   │   │   ├── en.js
│   │   │   └── uk.js
│   │   ├── lib/               # Shared helpers
│   │   │   ├── chat-filter.js
│   │   │   ├── dom.js
│   │   │   ├── errors.js
│   │   │   ├── order-toggle.js
│   │   │   └── pagination.js
│   │   ├── views/              # One controller per nav tab
│   │   │   ├── backfill.js
│   │   │   ├── chats.js
│   │   │   ├── deleted.js
│   │   │   ├── health.js
│   │   │   ├── messages.js
│   │   │   └── stats.js
│   │   ├── app.js              # Entry point, loaded as an ES module - routes between views
│   │   ├── archiver-toggle.js   # Start/stop control + status polling, shared by the nav rail and Backfill page
│   │   ├── i18n.js              # t(), language switching, dispatches televault:langchange
│   │   └── theme.js              # Light/dark theme toggle - loaded as a plain script, before the module graph
│   ├── icons/
│   │   ├── icon-192.png
│   │   └── icon-512.png
│   ├── favicon.ico
│   ├── index.html
│   ├── sw.js
│   └── manifest.webmanifest
├── docker-compose.yml    # Local dev Postgres
├── docker/init/
│   └── 01-extensions.sql   # Runs once, first time the Postgres container starts (enables pg_trgm)
├── utils/
│   ├── atomic_write.py    # Crash-safe JSON writes (used for the heartbeat + backfill-status files)
│   └── logging_setup.py   # Console + rotating file logging
├── .env.example        # Environment variable template
├── .gitattributes      # Enforces LF line endings on Windows checkouts
├── .gitignore
├── requirements.txt    # Pinned dependencies
├── CHANGELOG.md
└── README.md
```

---

## Routine maintenance

A few things worth checking on periodically once this is deployed and running long-term - nothing urgent, just good habits:

- **`docker system prune`** - Docker images/layers accumulate over time (old
  Postgres image versions, dangling build layers). Run
  `docker system prune` occasionally to reclaim disk space. This does **not**
  touch the `televault_pgdata` volume or your data (`docker system prune`
  never removes volumes unless you explicitly pass `--volumes` - avoid that
  flag). Check disk usage first with `docker system df` if you want to see
  what's actually being reclaimed before running it.
- **Database backups** - `docker compose exec postgres pg_dump -U televault televault > backup.sql`
  gives you a plain-text SQL dump you can restore from later. Worth
  automating (a cron job) once this is deployed somewhere that matters.
- **Disk usage on the VPS generally** - the archive only grows; check
  available disk space periodically (`df -h`), especially once media
  archiving lands (Phase 4 - see CHANGELOG).
- **`alembic current`** - shows which migration is currently applied. Useful
  after pulling updates, to confirm `alembic upgrade head` actually ran and
  the database schema matches what the code expects.

---

## Notes

- **`.session` file:** treat it like a password. It lets anyone run requests
  as your Telegram account. It's excluded from git via `.gitignore`.
- **Telegram ToS:** TeleVault archives only messages from chats you're already
  part of, for personal use. It doesn't automate sending, scrape public
  content, or interact with other accounts - it stays well within the
  acceptable personal-use boundary.
- **Media messages** (photos, stickers, voice notes) are silently skipped in
  Phase 1. The log will show a `DEBUG` line for each skipped message if you
  set `LOG_LEVEL=DEBUG` in `.env`.
- **Archive access is per-account, not instance-wide.** `/api/chats`,
  `/api/messages`, `/api/deleted`, `/api/stats`, and `/api/health` each
  resolve the calling account's own `archive_db_ref` (see
  `api/dependencies.py`'s `get_archive_connection()`) - there's no single
  "the" archive anymore, and no `OWNER_USER_ID` setting (removed; if you
  still have it in an old `.env`, it's ignored). `/api/telethon/*` and
  `/api/backfill/*` are different: exactly one account controls this
  running instance's one live userbot (`require_instance_owner()`), because
  that part of the architecture is still single-instance - see "Linking a
  Telegram account" above for the plan there. `POST /auth/register`,
  `/login`, `/refresh`, `/logout`, and `GET /auth/me` stay open to anyone
  with an invite, since those are what let an account prove who it is in
  the first place. Admin status (`is_admin`) governs account management
  only (currently just: creating other admins and invites via
  `scripts/manage_admin.py` - see "Inviting other people" above), never
  archive or Telegram access.
- **Local development needs Postgres running.** Unlike SQLite, there's no
  "just open the file" - the Postgres container (or however you're running
  Postgres) needs to be up any time you want to connect to the database,
  including via a GUI client or `psql`. See step 2's note above.