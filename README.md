# TeleVault

A personal Telegram userbot that archives all your messages in real time and
preserves deleted ones so you can retrieve them later — with a web UI to
browse, search, and review what's been archived.

**Phase 1 (userbot):** text messages only, all chat types, PostgreSQL storage.
**Phase 2 (web UI):** read-only REST API + installable PWA — Chats, Messages,
Deleted, Stats, and Health views, with EN/UK language support and light/dark themes.

> **Status:** PostgreSQL is now the storage layer (SQLAlchemy Core + Alembic
> migrations), replacing the SQLite-based storage from
> [`v1.2.0`](CHANGELOG.md#120--2026-09-07) and earlier. See the CHANGELOG for
> the full migration writeup and what's next.

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

---

## 4. Set up the database

With Postgres running (step 2), apply the schema:

```bash
alembic upgrade head
```

This creates all tables, indexes, and the `pg_trgm` extension used for
search. Safe to run again later after pulling new schema changes - Alembic
only applies migrations that haven't run yet.

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

The web UI is a separate process from the userbot — both can run at the same
time, reading/writing the same Postgres database (the API only ever reads,
via a read-only connection - see `api/dependencies.py`).

```bash
uvicorn api.server:app --host 127.0.0.1 --port 8000
```

Then open **http://localhost:8000** in a browser. You should see the Chats
view load first, with Messages, Deleted, Stats, and Health in the nav rail.

A few things worth knowing:

- **Installable as an app:** most browsers will offer to install it (via the
  address bar or browser menu) since it ships a PWA manifest and service
  worker. Installed or not, it works the same.
- **Offline behaviour:** the app shell (HTML/CSS/JS) is cached for offline
  loading, but data always requires a live connection — `/api/*` is
  deliberately excluded from the cache, since this is private data and a
  stale cached result would be misleading, not just old.
- **Theme and language:** toggle at the bottom of the nav rail (☀/☾ for
  theme, EN/UK for language) — on narrow/mobile screens, where the nav
  collapses to a top bar, they move to the right end of that bar instead.
  Both persist across visits via `localStorage`.
- **Interactive API docs:** available at `http://localhost:8000/api/docs`
  (Swagger UI) if you want to explore the endpoints directly.
- **Deployment note:** for always-on use, run this the same way as the
  userbot (systemd, etc.), with Nginx proxying `/api/*` to this process and
  serving `/` — see `api/server.py`'s docstring for the exact setup.

---

## Project structure

```
televault/
├── alembic/             # Schema migrations (Alembic) - source of truth is db/schema.py
│   └── versions/
├── api/                 # REST API (FastAPI) — read-only, serves web/ as static files
│   ├── routes/          # chats.py, messages.py, deleted.py, stats.py, health.py, backfill.py
│   ├── schemas/         # Pydantic v2 response models
│   ├── dependencies.py  # get_db() — read-only Postgres connection per request
│   └── server.py        # FastAPI app + static file mount
├── main.py              # Userbot entry point
├── config.py            # Settings loader (.env -> Settings dataclass)
├── db/
│   ├── connection.py    # Postgres connection pool (SQLAlchemy Engine + psycopg)
│   ├── schema.py        # Table definitions (SQLAlchemy Core) - read by Alembic, not applied at runtime
│   ├── queries.py       # All write operations (used by the userbot)
│   └── read_queries.py  # All read operations (used by the API)
├── scripts/
│   ├── migrate_sqlite_to_postgres.py  # One-time SQLite -> Postgres data migration
│   ├── toggle_archiver.ps1 / .bat     # Windows shortcut to start/stop the live archiver
├── handlers/
│   ├── helpers.py       # Shared Telethon entity utilities
│   ├── on_message.py    # NewMessage handler
│   ├── on_delete.py     # MessageDeleted handler
│   └── on_edit.py       # MessageEdited handler
├── web/                 # Vanilla JS/HTML/CSS PWA — no build step
│   ├── css/
│   ├── js/
│   │   ├── lib/         # Shared helpers (DOM escaping, pagination)
│   │   ├── views/       # One controller per nav tab
│   │   └── i18n/        # en.js, uk.js
│   ├── index.html
│   ├── sw.js
│   └── manifest.webmanifest
├── docker-compose.yml   # Local dev Postgres
├── docker/init/         # Runs once, first time the Postgres container starts (enables pg_trgm)
└── utils/
    └── logging_setup.py # Console + rotating file logging
```

---

## Routine maintenance

A few things worth checking on periodically once this is deployed and running long-term - nothing urgent, just good habits:

- **`docker system prune`** — Docker images/layers accumulate over time (old
  Postgres image versions, dangling build layers). Run
  `docker system prune` occasionally to reclaim disk space. This does **not**
  touch the `televault_pgdata` volume or your data (`docker system prune`
  never removes volumes unless you explicitly pass `--volumes` - avoid that
  flag). Check disk usage first with `docker system df` if you want to see
  what's actually being reclaimed before running it.
- **Database backups** — `docker compose exec postgres pg_dump -U televault televault > backup.sql`
  gives you a plain-text SQL dump you can restore from later. Worth
  automating (a cron job) once this is deployed somewhere that matters.
- **Disk usage on the VPS generally** — the archive only grows; check
  available disk space periodically (`df -h`), especially once media
  archiving lands (Phase 4 - see CHANGELOG).
- **`alembic current`** — shows which migration is currently applied. Useful
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
- **The web UI has no authentication.** `api/server.py` doesn't check any
  credentials — anyone who can reach the port can read your entire archive,
  including deleted messages. Fine for `127.0.0.1`-only local use; if you
  deploy this on a VPS reachable from the internet, put it behind something
  that authenticates first (e.g. Nginx with basic auth, a VPN, or an
  SSH tunnel) rather than exposing the port directly. Login/auth for the web
  UI itself isn't planned yet.
- **Local development needs Postgres running.** Unlike SQLite, there's no
  "just open the file" - the Postgres container (or however you're running
  Postgres) needs to be up any time you want to connect to the database,
  including via a GUI client or `psql`. See step 2's note above.