# TeleVault

A personal Telegram userbot that archives all your messages in real time and
preserves deleted ones so you can retrieve them later - with a web UI to
browse, search, and review what's been archived.

> **Disclaimer:** TeleVault is an independent, community-built project and is
> not affiliated with, endorsed by, or in any way officially connected to
> Telegram Messenger or Telegram FZ-LLC. Use of this software is at your own
> risk and responsibility.

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
- Telegram API credentials (free - takes two minutes to get; step 6 walks through it)

---

## 1. Set up the project

```bash
# Clone the repo
git clone https://github.com/gvneshv/TeleVault.git
cd TeleVault

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Start Postgres

```bash
docker compose up -d
```

This starts a local Postgres 16 instance in the background, matching the credentials
`.env.example`'s `CONTROL_DATABASE_URL` default already expects - no extra configuration needed for local dev.
Data persists in a Docker volume across restarts (`docker compose down` stops the container without touching
your data; only `docker compose down -v` wipes it).

**Note for local development:** Postgres is a client-server database - it needs to actually be running
(this container up) any time you want to connect to it at all, whether that's TeleVault itself, `psql`, or a
GUI client. `docker compose up -d` takes a couple of seconds and then just runs quietly in the background;
most people start it once at the beginning of a dev session and leave it running. See `docker-compose.yml`'s
comments for more.

---

## 3. Configure

```bash
cp .env.example .env
```

Fill in exactly these four values in `.env` - nothing else is required:

```
CONTROL_DATABASE_URL=postgresql+psycopg://televault:televault@localhost:5432/televault_control
FERNET_KEY=            # python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
JWT_SECRET=            # openssl rand -hex 32
TELEVAULT_BOOTSTRAP_ADMIN_TOKEN=   # any random phrase, e.g. openssl rand -hex 16
```

- `CONTROL_DATABASE_URL` already matches the container from step 2. It also tells TeleVault which Postgres
  server the per-account archive databases live on (same server and credentials, one database per account).
- `FERNET_KEY` encrypts every account's Telegram credentials and session in the database. **Back it up somewhere
  safe** - losing it makes those stored values permanently unreadable.
- `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` is what lets the *first* person register through the web UI (see step 5).
  It works exactly once, then is inert.

**There are no Telegram settings in `.env`.** Every account - yours included - enters its own Telegram API
credentials and signs in through the web UI (step 6), and they are stored encrypted in the control database.

---

## 4. Create the control database schema

```bash
alembic -c alembic_control.ini upgrade head
```

This creates the accounts / invites / sessions / audit-log tables. Safe to run again after pulling updates -
Alembic only applies migrations that haven't run yet. (There is no equivalent step for archives: each account's
archive database is created *and* migrated automatically when you finish setup in step 6.)

---

## 5. Start the web UI and create your account

```bash
uvicorn api.server:app --host 127.0.0.1 --port 8000
```

Open **http://localhost:8000**, go to **Register**, and use the value of `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` as the
invite token. That account becomes TeleVault's one and only admin. Everyone after you registers with an invite
you create in the admin panel.

> Prefer a shell? `python scripts/manage_admin.py create --username youruser` creates the admin account directly
> (no bootstrap token needed), and `python scripts/manage_admin.py create-invite --created-by youruser` prints an
> invite token - handy as a recovery path if the web UI is ever unusable.

`--host 0.0.0.0` (instead of `127.0.0.1`) makes it reachable from other devices on your network too (e.g. testing
from a phone). For raw API exploration set `ENABLE_API_DOCS=true` in `.env`, restart, and open
**http://localhost:8000/api/docs** (off by default - see `.env.example` for why).

---

## 6. Link Telegram and create your archive

In the web UI open **Settings -> Telegram setup**:

1. **Get your Telegram API credentials** - go to **https://my.telegram.org**, log in with your phone number, open
   **"API development tools"**, and create an app (any name and short name - they're just labels). Copy the
   **api_id** (a number) and **api_hash** (a hex string) into the form. They are tied to *your* Telegram account;
   treat them like passwords.
2. **Enter your phone number** - Telegram sends a login code to your Telegram app.
3. **Enter the code** (and your two-step-verification password, if you have one).
4. **Create archive database** - provisions your own Postgres database and applies the schema. This needs the
   Postgres role behind `CONTROL_DATABASE_URL` to have `CREATEDB` (the Docker setup already does; a deliberately
   least-privileged production role may not - you then get a clear `provisioning_permission_denied` error. Either
   grant it with `ALTER ROLE <role> CREATEDB;`, or create and migrate a database yourself with
   `alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head` and point your account at it with
   `python scripts/manage_admin.py set-archive --username youruser --db-name DBNAME`).

The same steps exist as plain API calls (`POST /api/telegram/credentials`, `/telegram/link/send-code`,
`/telegram/link/confirm`, `POST /api/archive/provision`) if you'd rather script them.

---

## 7. Start archiving

Use the archiver toggle in the nav rail (or the **Backfill** tab for history). That starts a worker process for
*your account* - `python main.py --user-id <your id>` under the hood - which logs in with the session you saved in
step 6. Every account runs its own worker, so several accounts can archive at the same time.

You can also run a worker by hand (useful for debugging); your account id is shown in the admin panel:

```bash
python main.py --user-id 1
python backfill.py --user-id 1 --chat @someusername --limit 500
```

If a worker can't start it says why and exits - e.g. the account hasn't linked Telegram yet, or the saved session
was revoked from Telegram's **Devices** list (just sign in again in step 6). Workers never ask for a login code.

You should see output like:

```
2026-05-12 18:00:00  INFO      utils.logging_setup    Logging initialised - level=INFO
2026-05-12 18:00:01  INFO      __main__               Starting TeleVault archiver for account id=1...
2026-05-12 18:00:02  INFO      __main__               Authenticated as: Alice (id=123456789)
2026-05-12 18:00:02  INFO      __main__               Event handlers registered.
2026-05-12 18:00:02  INFO      __main__               TeleVault is running. Press Ctrl-C to stop.
```

From this point, TeleVault is archiving every text message in real time.

---

## 8. Smoke test

With the archiver running, open Telegram on your phone or desktop and:

1. **Send yourself a message** (open Saved Messages and type anything). You should see a log line:
   ```
   INFO  db.queries  Inserted message 1 from chat 123456789 -> internal id 1
   ```

2. **Delete that message.** You should see:
   ```
   INFO  db.queries  Flagged message 1 in chat 123456789 as deleted at ...
   ```

3. **Check the web UI** - the message appears under *Messages*, and under *Deleted* once flagged. Or query your
   archive database directly (its name is `televault_archive_<your id>`):
   ```bash
   docker compose exec postgres psql -U televault -d televault_archive_1 -c "
     SELECT text, is_deleted, deleted_at
     FROM messages
     ORDER BY archived_at DESC
     LIMIT 5;
   "
   ```
   (Or use any Postgres GUI client - DBeaver, pgAdmin, TablePlus, or a VSCode Postgres extension - pointed at
   `localhost:5432` with the credentials from `.env`.)

---

## 9. Stopping

Use the archiver toggle in the web UI, or press **Ctrl-C** in the terminal of a worker you started by hand. The
shutdown is graceful - the database connection is flushed and closed cleanly before the process exits.

---

## Migrating an existing SQLite archive

If you have an archive from a previous SQLite-based install (`v1.2.0` or earlier) that you want to keep, migrate
it into an account's archive database (created in step 6) *before* that account's archiver first runs:

```bash
python scripts/migrate_sqlite_to_postgres.py --sqlite-path data/televault.db --target-db televault_archive_1 --dry-run   # preview, writes nothing
python scripts/migrate_sqlite_to_postgres.py --sqlite-path data/televault.db --target-db televault_archive_1             # the real migration
```

It reads the SQLite file read-only (your old file is never modified). Safe to interrupt and re-run - see the
script's own module docstring for how. It verifies its results (row counts, source vs. target) at the end and
exits with an error if anything doesn't match. Starting fresh? Skip this.

---

## About the web UI

A few things worth knowing:

- **Installable as an app:** most browsers will offer to install it (via the address bar or browser menu) since it
  ships a PWA manifest and service worker. Installed or not, it works the same.
- **Offline behaviour:** the app shell (HTML/CSS/JS) is cached for offline loading, but data always requires a live
  connection - `/api/*` is deliberately excluded from the cache, since this is private data and a stale cached
  result would be misleading, not just old.
- **Theme and language:** toggle at the bottom of the nav rail (☀/☾ for theme, EN/UK for language) - on
  narrow/mobile screens, where the nav collapses to a top bar, they move to the right end of that bar instead.
  Both persist across visits via `localStorage`.
- **A freshly-registered account shows "hasn't been set up yet" on most tabs - that's expected**, not a bug: those
  views need an archive (step 6) before there's anything to show.
- **Deployment note:** for always-on use, run the API under systemd (or similar), with Nginx proxying `/api/*` to
  it and serving `/` - see `api/server.py`'s docstring for the exact setup. Workers are started and stopped by the
  API, so there is nothing else to supervise.

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
│   ├── dependencies.py        # get_archive_connection() - the CALLING account's read-only archive connection; require_archiving_ready, require_admin
│   ├── process_utils.py       # per-account single-instance guard + heartbeat-file reading, shared by the workers and the telethon status route
│   └── server.py              # FastAPI app + static file mount
├── backfill.py                # Historical-message import for one account (--user-id N); can't run at the same time as that account's live archiver (same Telegram session)
├── main.py                    # Live archiver for one account (--user-id N)
├── config.py                  # Settings loader (.env -> Settings dataclass)
├── db/
│   ├── connection.py          # Postgres connection pool (SQLAlchemy Engine + psycopg)
│   ├── schema.py               # Table definitions (SQLAlchemy Core) - read by Alembic, not applied at runtime
│   ├── queries.py               # All write operations (used by the userbot)
│   └── read_queries.py          # All read operations (used by the API)
├── scripts/
│   ├── migrate_sqlite_to_postgres.py  # One-time SQLite -> Postgres data migration
│   ├── manage_admin.py                # Admin/invite management from a shell (recovery path)
│   └── toggle_archiver.ps1 / .bat     # Windows shortcut to start/stop one account's live archiver (-UserId N)
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
│   ├── account.py         # Loads ONE account's decrypted Telegram credentials + archive db name from the control DB (used by the workers)
│   ├── worker_paths.py    # Per-account heartbeat / backfill-status file locations (data/users/<id>/)
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
- **Database backups** - the control database holds every account's (encrypted) Telegram session:
  `docker compose exec postgres pg_dump -U televault televault_control > backup.sql` gives you a plain-text SQL dump.
  Back up your `FERNET_KEY` separately - the dump is useless without it. Archive databases
  (`televault_archive_<id>`) can be dumped the same way. Worth automating (a cron job) once this is deployed somewhere
  that matters.
- **Disk usage on the VPS generally** - the archive only grows; check
  available disk space periodically (`df -h`), especially once media
  archiving lands (Phase 4 - see CHANGELOG).
- **`alembic current`** - shows which migration is currently applied. Useful
  after pulling updates, to confirm `alembic upgrade head` actually ran and
  the database schema matches what the code expects.

---

## Notes

- **Terms of Service and Privacy Policy:** served at `/tos` and `/privacy`,
  linked from every page's footer and from the mandatory registration
  checkbox. The tracked `web/tos.html` and `web/privacy.html` are
  **placeholder templates** (highlighted `[PLACEHOLDER]` marks) so a fresh
  clone works. To deploy: copy both to `instance/legal/` (git-ignored, so your
  real name/contact/jurisdiction never reach the public repo), fill in every
  placeholder there, and keep your own backup of those copies - they are not
  in git. The server prefers `instance/legal/<name>.html` and logs a warning
  at startup while it is still serving a template. When a document changes
  materially, update its "Last updated" line and bump `CURRENT_TERMS_VERSION`
  in `api/routes/auth.py`. Acceptance (time + version) is recorded per account;
  run `alembic -c alembic_control.ini upgrade head` once to add those columns.
- **Telegram sessions:** an account's saved login is stored encrypted in the control database (there is no `.session`
  file any more). Treat the database dump, `FERNET_KEY` and the database credentials like passwords - together they let
  anyone act as your Telegram account. A legacy `televault.session` file from an older install is no longer used:
  delete it, and terminate that session under Telegram -> Settings -> Devices if you no longer need it.
- **Telegram ToS:** TeleVault archives only messages from chats you're already
  part of, for personal use. It doesn't automate sending, scrape public
  content, or interact with other accounts - it stays well within the
  acceptable personal-use boundary.
- **Media messages** (photos, stickers, voice notes) are silently skipped in
  Phase 1. The log will show a `DEBUG` line for each skipped message if you
  set `LOG_LEVEL=DEBUG` in `.env`.
- **Everything is per-account.** `/api/chats`, `/messages`, `/deleted`, `/stats`, `/health` and the
  `/api/telethon/*` and `/api/backfill/*` controls each act on the calling account's own archive (see
  `api/dependencies.py`'s `get_archive_connection()`), Telegram session, worker processes, heartbeat and status
  files - there is no "instance owner" and no instance-wide archive. Starting a worker additionally requires finished
  setup (Telegram linked + archive created; otherwise 409 `telegram_not_linked` / `archive_unattached`).
  `POST /auth/register`, `/login`, `/refresh`, `/logout`, and `GET /auth/me` stay open to anyone with an invite, since
  those are what let an account prove who it is in the first place. Admin status (`is_admin`) governs account
  management only (the admin panel and `scripts/manage_admin.py`), never archive or Telegram access.
- **Local development needs Postgres running.** Unlike SQLite, there's no
  "just open the file" - the Postgres container (or however you're running
  Postgres) needs to be up any time you want to connect to the database,
  including via a GUI client or `psql`. See step 2's note above.

## Fonts

The UI uses Tektur, Sofia Sans and IBM Plex Mono, self-hosted from `web/fonts/`
(subsetted to Latin + Cyrillic; declared in `web/css/fonts.css`). All three are
licensed under the SIL Open Font License 1.1 - the license texts sit next to the
files and must stay with them. To change a typeface, replace the woff2 file(s),
update `fonts.css` and the `--font-*` tokens in `web/css/variables.css`, and add
the new paths to `SHELL_FILES` in `web/sw.js` (and bump `CACHE_NAME`).

## License

TeleVault is source-available under the **PolyForm Noncommercial License 1.0.0**
(see [LICENSE](LICENSE)): you may use, modify and share it for noncommercial
purposes; commercial use needs a separate agreement with the author. The
software is provided without warranty and without liability. Anyone who
distributes it must keep the license text (or its URL) and the
`Required Notice:` line in [NOTICE](NOTICE).