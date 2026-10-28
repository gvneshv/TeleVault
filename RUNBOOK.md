# TeleVault operator runbook

For whoever stands up and looks after a TeleVault instance. The README is the quick start; this is the reference you
come back to when something needs generating, finding, restoring or recovering. Everything here was taken from the
code (`config.py`, `utils/worker_paths.py`, `api/process_utils.py`, `scripts/manage_admin.py`); where something has
**not** been exercised end to end it says so.

---

## 1. Secrets and settings

All settings come from `.env` (copy `.env.example`). Nothing Telegram-related lives there: each account's `api_id`,
`api_hash` and login session are stored Fernet-encrypted in the control database.

| Key | Required | Default | How to generate / what it does |
|---|---|---|---|
| `CONTROL_DATABASE_URL` | no | `postgresql+psycopg://televault:televault@localhost:5432/televault_control` | Control DB. Also tells TeleVault which Postgres *server* the per-account archives live on (same server, same credentials). |
| `FERNET_KEY` | **yes** | - | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Encrypts the Telegram columns. **Lose it and those values are unrecoverable.** |
| `JWT_SECRET` | **yes** | - | `openssl rand -hex 32`. Signs access/refresh tokens. Rotating it logs everyone out but does not touch encrypted data. |
| `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` | see note | unset | `openssl rand -hex 16`. Lets the first person register as admin through the web UI. Works once, inert afterwards. |
| `ENABLE_API_DOCS` | no | `false` | `true` serves `/api/docs`, `/api/redoc`, `/api/openapi.json`. Keep `false` on anything other people can reach. |
| `DATA_DIR` | no | `data` | Runtime-file root (section 3). A relative path is resolved against the repo root, not the working directory. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Applies to the API server and to every worker. |
| `LOG_FILE` | no | console only | e.g. `logs/televault.log` for rotating file logs. |

Note on the bootstrap token: every account comes from an invite and only the admin makes invites, so a fresh instance needs
either this token or `python scripts/manage_admin.py create`. If neither is in place the API server refuses to start and
says so. Once an admin exists the token is inert and may be removed.

Keep `.env` out of git. Back up `FERNET_KEY` separately from any database dump: a dump without it is useless, and
a dump together with the key and the DB credentials lets someone act as your Telegram account.

---

## 2. Databases

| Database | Name | Created by | Migrated by |
|---|---|---|---|
| Control (accounts, invites, refresh tokens, audit log, encrypted Telegram credentials) | `televault_control` (from `CONTROL_DATABASE_URL`) | you / your Postgres setup | `alembic -c alembic_control.ini upgrade head` (re-run after every update; only unapplied migrations run) |
| One archive per account | `televault_archive_<user_id>` (plus a Postgres role of the same name that alone can open it) | the web UI's "Create archive database" (`POST /api/archive/provision`, `db/provisioning.py`) | the same step runs `alembic upgrade head` against it |

The Postgres role behind `CONTROL_DATABASE_URL` needs `CREATEDB` and `CREATEROLE` for provisioning (the Docker one is a superuser and has both).
The Docker image creates only `televault_control` (`POSTGRES_DB`). A volume created by an older compose file also contains an unused empty `televault` database; remove it with
`docker compose exec postgres psql -U televault -d televault_control -c "DROP DATABASE televault;"` (it cannot be dropped while connected to it, hence `-d televault_control`), or ignore it.
Changing `POSTGRES_DB` has no effect on an existing volume - the image only reads it when it first initialises an empty one.

Manual archive, if provisioning isn't possible:

```bash
alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head
python scripts/manage_admin.py set-archive --username youruser --db-name DBNAME
python scripts/secure_archives.py     # gives it its own login (see "Who can open an archive" below)
```

`alembic current` (with the matching `-c` / `-x`) shows what is applied.

### Who can open an archive

Every archive has its **own Postgres role** (named like the database, e.g. `televault_archive_3`): it owns that one database, has no other privilege, and Postgres refuses every
other role a connection to it (`CONNECT` is revoked from `PUBLIC`; the same is done once for the control database, which holds password hashes and Telegram sessions).
So if the application ever picked the wrong database, or one account's password leaked, Postgres itself would say "permission denied" instead of handing over someone else's messages.
The role's password is random, shown to nobody, and kept **Fernet-encrypted in the control database** (`users.archive_db_password`), like the Telegram sessions - so **back up `FERNET_KEY`**
as well. If it is ever lost, the stored passwords cannot be read and the archives show as unavailable; `python scripts/secure_archives.py --repair` then resets every unusable login
(it needs the server login, which can reset any role, and never touches the data).
The login in `CONTROL_DATABASE_URL` stays all-powerful: it is the server's own login (needed to create archives, run `migrate_archives.py` for older archives and manage the control database).

Archives created **before** this feature have no role yet and keep working with that server login. Give them one, once:

```bash
python scripts/secure_archives.py --dry-run   # which archives are not secured yet (changes nothing)
python scripts/secure_archives.py             # secure them: create the role, hand it the tables, prove it works, then store the password
```

Run it from the repo root with the virtualenv active and the same `.env` as the API server (needs `CONTROL_DATABASE_URL` and `FERNET_KEY`). The data is not touched - only who owns it.
It is safe to repeat, never prints a password, and carries on past an archive that fails (exit status 1). **Restart the archiver and backfill workers afterwards** so they pick up the
new logins (the API server needs nothing). Deleting an account drops its role together with its database. An archive whose stored login stops working is reported as unavailable -
the app never silently falls back to the server login.

Two caveats. Postgres's own log records the `CREATE ROLE ... PASSWORD` statement if `log_statement` is `ddl` or `all`; keep it at the default (`none`) on a production server.
And a non-superuser server login only closes the control database to the archive roles if it owns that database; otherwise have its owner run
`REVOKE CONNECT ON DATABASE <control> FROM PUBLIC;` once.

### Updating existing archives

A new release can add a migration to the archive schema (`alembic/versions/`). **Nothing applies it to archives that already exist** - only a freshly provisioned one gets the whole chain.
After pulling an update, run it once per archive:

```bash
python scripts/migrate_archives.py --dry-run   # only report which archives are out of date
python scripts/migrate_archives.py             # migrate them
```

Run it from the repo root with the virtualenv active and the same `.env` as the API server. There is **no password to look up or type**: the script reads `CONTROL_DATABASE_URL` and
`FERNET_KEY`, asks the control database which archives exist, and migrates each as that archive's own role (an archive not yet secured, see above, is reached with the server login).
It prints no URL or password.
It is safe to repeat and to run while the app is running (only unapplied migrations run, and so far they only add tables). If one archive fails (say its database was removed by hand)
it says which and carries on; the exit status is then 1.

Advanced fallback, for a single archive: `alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head`, with USER and PASS taken from `CONTROL_DATABASE_URL`
(the server login works on every archive, secured or not, as long as it is a superuser).
Until an archive is migrated nothing breaks - the code that needs a new table treats its absence as "nothing configured yet" - but the matching feature is unavailable
(for the ingest blocklist, Settings -> Chats not archived says the archive needs updating, and the archiver logs one warning).

---

## 3. Per-account runtime layout

The API server spawns one live archiver (`main.py --user-id N`) and, on demand, one backfill
(`backfill.py --user-id N`) **per account**. They never talk to the API directly; they coordinate through two small files:

```
<DATA_DIR>/users/<user_id>/televault.heartbeat    live archiver, rewritten every 20 s while connected
<DATA_DIR>/users/<user_id>/backfill_status.json   backfill progress (written atomically via a .tmp file + rename)
```

- **Heartbeat** holds `{pid, updated_at}`. The archiver counts as running only if `updated_at` is under 60 s old
  (three missed beats), so a crashed worker's leftover file can never block a restart.
- **Backfill status** holds a `pid`; it counts as running only if that process is actually alive, so it self-heals too.
- Both are per account, so one account's worker never blocks another's. A single account cannot run archiver and
  backfill at once (one Telethon session, one connection).
- Both directories are created on first write. `DATA_DIR` must be writable by the API server (the user it runs as).
- Safe to delete by hand: a stale heartbeat or status file for an account whose worker is not running.
  Do not delete them while the worker is running.

Running a worker by hand (debugging) enforces the same guards:

```bash
python main.py --user-id 1
python backfill.py --user-id 1 --chat @someusername --limit 500
```

Workers never prompt for a login code. If the saved session is revoked (Telegram -> Settings -> Devices) or
corrupted they exit with a message; sign in again under Settings -> Telegram setup.

---

## 4. Recovery with `scripts/manage_admin.py`

Run on the server by someone who already has database access. Never reachable over HTTP. Passwords are always prompted
(hidden), never taken from the command line.

| Situation | Command |
|---|---|
| No admin exists and no bootstrap token was set | `python scripts/manage_admin.py create --username NAME` |
| Locked out of the UI, need an invite | `python scripts/manage_admin.py create-invite --created-by NAME [--expires-hours 48]` |
| Archive DB exists but the account doesn't point at it | `python scripts/manage_admin.py set-archive --username NAME --db-name DBNAME` |
| Remove the admin completely (**irreversible**: account, archive database, Telegram credentials) | `python scripts/manage_admin.py delete-admin --username NAME` (you retype the username to confirm) |

There is exactly one admin, enforced by the database. There is no promote command.

---

## 5. Backups

**Not automated yet** - there is no backup script in the repo. Manual commands (Docker setup):

```bash
docker compose exec postgres pg_dump -U televault televault_control   > control_$(date +%F).sql
docker compose exec postgres pg_dump -U televault televault_archive_1 > archive_1_$(date +%F).sql
```

Back up with it: `.env` (or at least `FERNET_KEY`), and your filled-in legal pages (`instance/legal/`, git-ignored).
Restoring a control dump without the original `FERNET_KEY` leaves every stored Telegram credential unreadable.
A restore procedure has not been written or rehearsed; do that before relying on any backup.

---

## 6. Routine checks

- Docker disk use: `docker system df`, then `docker system prune` occasionally. Never pass `--volumes`; that is what would delete the database volume.
- Disk space (`df -h`): archives only grow.
- After pulling updates: `pip install -r requirements.txt -c constraints.txt` (exact tested versions), then `alembic -c alembic_control.ini upgrade head`. Existing archive databases are migrated per account, not by that command (see "Updating existing archives" in section 2).
- Legal pages: while `instance/legal/` is missing, the server logs a warning at startup and serves the placeholder templates.