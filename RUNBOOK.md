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
| `TELEVAULT_BACKUP_RECIPIENT` | no | unset | age **public** key(s), comma-separated, that `scripts/backup.py` encrypts to (section 5). Never the private key. |

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
For the archive owner table (own messages on the right in the chat view), migrate and then **restart the archiver once**: it records your Telegram id when it starts. Until then every message is drawn on the left.
For the message ignore rules (Settings -> Ignore rules), migrate and you are done - no restart: the archiver notices the new tables on the next text message. Until then nothing is ignored, the archiver logs one warning, and the card says the archive needs updating.
For the "Unread messages" bar in the chat view, migrate and then **restart the archiver once**: it copies your Telegram reading positions into the new `chats.read_inbox_max_id` column when it starts, and follows reads you make on other devices afterwards. Until then the chat view draws no bar, and an un-migrated archive makes the archiver log one warning. A read made while the archiver was stopped or disconnected shows up at its next start. TeleVault only listens to Telegram's read state; it never marks anything as read.
For the "Dismiss" button on the unread bar, migrate (one more column on `chats`; instant) - no restart needed: it only stores a number in the archive and never marks anything as read in Telegram. Until migrated it answers 409 and the bar stays.
For the check marks (read receipts) on your own messages in private chats, migrate and then restart the archiver once: it fills the new column from Telegram's chat list at start-up and keeps it up to date from live events. Only private chats are tracked, one number per chat, no times; nothing is sent to Telegram. A person who hides their read receipts shows one tick forever.
For the automatic cleanup (Settings -> Automatic cleanup), migrate (one new small table) and then **restart the archiver once**: the cleanup runs inside the archiver, once a day, and only while the archiver is running. It does nothing until you turn it on; turning it on needs a preview of how many messages would be deleted (irreversible). A run that would delete far more than expected is blocked and deletes nothing until you save again; the archiver's log then says so (counts only). Until migrated, the card says the archive needs updating and the archiver logs one line.
For the unread badges in the chat list, migrate (`migrate_archives.py` adds one index on `messages`; building it blocks writes for a moment, so run it with the archiver stopped) and you are done - no restart beyond the one for the bar above, whose reading position the badges use. The badge counts what is in the *archive*: it can be lower than the number in the Telegram app (ignored, blocked-chat and pre-archive messages are not counted). Until migrated, an archive still shows the badges (slower on very large archives).
For incremental backfill (a repeat backfill only fetches what is newer), migrate (one more column on `chats`; instant) - no restart needed, backfill is a separate process. The first backfill of each chat after migrating is a full walk (nothing records how far earlier runs got), later ones are incremental. Until migrated, every backfill walks every chat in full and logs one warning.
For the sender-name tables (your own names for senders, and the names seen over time), migrate and you are done - no restart: the archiver notices the new table on the next message. Name changes are only recorded from then on, and saving a name before the migration answers "archive needs updating".

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

### Does stopping the API server stop the archivers?

Not by TeleVault's design: the API's shutdown code only closes its database pools (`api/server.py`, `lifespan`), and logging out of the web UI only ends your web session.
Workers are separate processes that keep archiving; that is also why the Start/Stop button still works after the API was restarted (it goes through the heartbeat file).

What you may still see is the operating system's doing, not TeleVault's: pressing **Ctrl+C in the terminal that started uvicorn** is delivered to every process sharing that terminal,
including archivers the API launched from it, so they stop too. I believe this is how both Linux and Windows consoles behave, but it has not been verified on Windows here.
Stopping uvicorn another way (a service restart, `kill` on its PID, closing only the API) can leave the archivers running.
Either way nothing is lost: a stopped archiver simply does not store messages while it is off, and the heartbeat goes stale after 60 s, so it can be started again from the UI.

Running a worker by hand (debugging) enforces the same guards:

```bash
python main.py --user-id 1
python backfill.py --user-id 1 --chat @someusername --limit 500
```

Backfill goes gently on purpose: 1 s between pages of 100 messages, a random 2-5 s between chats, and when Telegram says "wait N seconds" it waits and retries (the web UI shows "waiting N s").
A wait over an hour stops the run with state `error` - start it again later; what was archived is kept and the next run continues from there. A big first backfill of hundreds of chats therefore takes a while by design.
Messages with a Telegram auto-delete timer or view-once media are never archived, by the archiver or by backfill.

**Choosing the chat.** The start dialog has a dropdown: "All chats (except blocked ones)" first, then your private chats, groups and channels, by name. A private chat shows the name you gave that person in the chat view. Blocked chats are not listed. The list is Telegram's own chat list, which the live archiver writes to `data/users/<id>/dialogs.json` about a minute after it starts and again every 6 hours, so: (1) a fresh install, or an archiver that has not run since this update, shows "All chats" only until the archiver has been running for a minute; (2) a chat you joined in the last hours may be missing until the next refresh (the file holds only chat ids, Telegram's names and chat types, nothing else; delete it to hide the list until the next refresh); (3) the list stays available while the archiver is stopped, which a backfill requires. From the command line `--chat` still takes a numeric id or a `@username`; a numeric id is now looked up in your chat list first.

**Cooldown: one backfill per period.** After a backfill *completes*, the next one (any chat, from the web UI or the command line) is refused until `BACKFILL_COOLDOWN_HOURS` have passed (default 24; `0` switches it off; at most 168 = a week). The period counts from the end of the account's last completed run. A run that was cancelled, stopped by a long flood wait, or that gave up on a chat after repeated flood waits does **not** start it, so continuing such a run is always allowed. The web UI answers with the time left; the command line prints it and exits without contacting Telegram. Reason: bulk history reads are what Telegram's flood limits exist for, and abuse can get the shared API id restricted for every account that uses it.

**Files of messages archived before media support.** After migrating (`python scripts/migrate_archives.py`), the first backfill that looks at files ("Messages and files" or "Files only") walks each chat from its first message once - slow, many requests - and gives every already-archived message that carries a file its record; nothing is downloaded by the backfill itself. Start the live archiver afterwards: it downloads those files one at a time, within the limits above. Messages already recorded are never reset. Later runs are incremental again. `python backfill.py --user-id 1 --full` forces a full walk on demand.

**What to fetch, and which file types.** The start dialog's "What to fetch": *Messages and files* (default), *Text only* (messages are saved, their files are recorded but not downloaded, a message with no text is not saved) or *Files only* (just messages that carry a file). Command line: `--mode all|text|media`. The two newer modes need the archive migrated and refuse to start otherwise. Which file *types* are downloaded (photos, videos, voice, audio, other files) is **Settings -> Files**, one choice for the whole account, applied to the live archiver and every backfill. A switched-off type is still recorded ("Photo" shows in the chat) but not downloaded; switching it back on queues the files that were held back (including those a text-only run set aside). Files already saved are never deleted by this.

**When files are deleted from the disk.** A message deleted in Telegram keeps its file - preserving it is the point of the archive. A file is removed only when its message is removed from the *archive*: by automatic cleanup (months setting), by "Delete this chat", or by "Delete all stored data" (the database change commits first, then the files go). Anything left behind by a crash or an interrupted download is removed by a sweep each time the archiver starts (`Removed N media file(s) that no archived message refers to.` in the log): files that no stored record refers to, and leftover `.partial` files. Only files named by number in `data/users/<id>/media/` are ever touched. Disk space freed this way counts against `MEDIA_ACCOUNT_LIMIT_GB` again immediately.

**Reading a backfill's log.** After each chat the log says why messages were skipped, e.g. `skipped 111: 111 older than the retention cutoff` or `... already archived` or `... a self-destruct timer`. If the log says "Automatic cleanup is ON", messages older than the cleanup period are deliberately not imported (and the archiver deletes any that are). A chat in Telegram with an auto-delete timer has every message skipped by design.

**Trying refused or failed files again.** A file marked `too_large`, `disk_full` or `failed` stays that way until you ask: after raising `MEDIA_MAX_FILE_MB` or `MEDIA_ACCOUNT_LIMIT_GB` (restart the archiver, settings are read at startup) or freeing disk space, call `POST /api/media/retry` (empty JSON body `{}`, or `{"statuses": ["disk_full"]}` for some of them). It only puts the rows back in the queue; the archiver does the downloading, and a file still over a limit is refused again without any request to Telegram. The same is a **Try again** button in Settings -> Files, shown when something was refused or failed, under a bar that shows how much of `MEDIA_ACCOUNT_LIMIT_GB` is used.

### Downloaded media (photos, videos, voice messages, audio, files)

**Seeing them.** The chat view shows a message's file above its text: photos appear by themselves as they scroll into view, videos, voice messages and audio play after one click, other files offer a download (the original file name is not stored). A file that was not saved says why (waiting, switched off in Settings, too large, storage full, no longer on Telegram, failed). The server decides from the file's own bytes what may be shown in the page; HTML, SVG and anything unknown is only ever offered as a download. A browser/PWA cannot tell the app's access token to an `<img>`, so files are fetched with the token and shown from memory: opening a chat with many photos downloads them from your own server as you scroll.

While the archiver runs it downloads the files of archived messages in the background, one at a time with a pause after each, into `<DATA_DIR>/users/<id>/media/` (one file per attachment, named by a number, never by anything Telegram supplied). The work list is the database (`message_media` rows with status `pending`), so a restart or a Telegram "wait N seconds" loses nothing: unfinished files are fetched on the next start.
Two limits per account, both in `.env`: `MEDIA_MAX_FILE_MB` (default 50: a larger file is not downloaded) and `MEDIA_ACCOUNT_LIMIT_GB` (default 10: once the stored files add up to this, nothing more is downloaded). The message text is archived either way and the row records why (`too_large`, `disk_full`). Those are the project's starting numbers, not Telegram's: size them to your disk. A refused file is NOT retried automatically, so raising a limit later does not fetch what was already refused (a later step adds that).
Watch the disk: `du -sh data/users/*/media`. If the disk itself fills up the row is marked `disk_full` and the log says so; free space and continue. Files are not yet part of `scripts/backup.py` (removal with the message is done; backup is still to come).

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

## 5. Backups and encryption

### What is encrypted, and where

| Layer | Protects against | How |
|---|---|---|
| Telegram `api_id` / `api_hash` / session, and each archive's database password | Someone who can read the control database (a leaked dump, a SQL injection) | Fernet (`FERNET_KEY`), per column, done by the app. |
| The database files and `data/` on the server | A stolen or discarded disk | **You provide it:** an encrypted volume (LUKS/BitLocker/FileVault, or the encrypted disk of a VPS provider). TeleVault does not do this itself. |
| Network access to PostgreSQL | Anyone on the internet reaching the database | The repo's `docker-compose.yml` publishes port 5432 on the machine's **loopback only** (`127.0.0.1:5432:5432`), so nothing outside the machine can reach it. Do not change that to `5432:5432`: Docker writes its own firewall rules, so a host firewall such as `ufw` does **not** stop a published port. On a server also set a real `POSTGRES_PASSWORD` in `.env` **before the first `docker compose up`** (Postgres reads it only when it first creates the data volume) and put the same password in `CONTROL_DATABASE_URL`. |
| Backups | A copy that leaks (cloud folder, USB stick, stolen laptop) | `scripts/backup.py` (below): public-key encryption, so the server cannot decrypt its own backups. |

Message text and media are deliberately **not** encrypted row by row or file by file: search is a trigram index on `messages.text`, which per-row encryption would break. Storage-level encryption is the chosen design.
It protects a disk that is switched off; it does **not** protect a running server whose operator (or hosting provider) can read the disk. Whoever hosts the machine can read its memory and disk while it runs - keep that in mind before choosing a managed or shared host.

### Making a backup

One-time setup, on a machine you trust (**not** the server): `age-keygen -o televault-backup-key.txt`. It prints a public key starting `age1...`. Keep the key *file* offline (password manager, safe drive): without it no backup can ever be opened, and nobody can recover it for you.
Give the server only the public key: `TELEVAULT_BACKUP_RECIPIENT=age1...` in `.env` (or `--recipient`). The script refuses a private key. Install the `age` program and PostgreSQL's `pg_dump` (the `postgresql-client` package; **its major version must not be older than the server's**), or use `--docker-service postgres`.

```bash
python scripts/backup.py --out /path/on/ANOTHER/disk       # creates <out>/<UTC timestamp>/ with one encrypted file per database + MANIFEST.sha256
python scripts/backup.py --docker-service postgres         # same, running pg_dump inside the compose service
```

- It never writes a readable dump to disk, keeps the newest 14 complete backups (`--keep N`) and removes older ones only after a fully successful run. A folder **without** `MANIFEST.sha256` is a failed or unfinished run: it is not a backup. Exit status 1 means at least one database failed.
- Run it from cron / Task Scheduler (daily is reasonable for an archive that only grows). Scheduling is not set up for you.
- A backup on the **same disk** as the database does not survive that disk dying. Copy the folder somewhere else (it is encrypted, so a cloud folder is acceptable).
- **Not in the backup, store separately:** `.env` (above all `FERNET_KEY` - a restored control database without it cannot decrypt a single Telegram session) and your filled-in legal pages (`instance/legal/`, git-ignored). Downloaded media (`data/`) is not backed up by the script yet; media archiving is still being built, and this section will say so when that changes.

### Checking a backup (do this now and then)

On the machine that holds the private key, with a copy of the folder (it needs the repo and its virtualenv, but **no `.env`**):

```bash
python scripts/backup.py --verify <folder> --identity televault-backup-key.txt
```

It checks every file against `MANIFEST.sha256`, decrypts it and makes sure `pg_restore` can read it. Nothing is restored and nothing is written. An unchecked backup is a hope, not a backup.

### Restoring (rehearsed once, see the end of this section for what that covers)

Order matters: control database first, then each archive, then re-create the archive logins. Use a **new, empty** server or new database names; never restore over a database you still need.

```bash
# 1. control database (database name = the one in CONTROL_DATABASE_URL).
#    With the compose Postgres on a fresh volume it already exists, empty: skip the createdb line (createdb would only say "already exists").
createdb -h localhost -U televault televault_control
age -d -i televault-backup-key.txt control.dump.age | pg_restore --no-owner --no-privileges -h localhost -U televault -d televault_control

# 2. each archive: the database name is in the file name, archive_<name>.dump.age
createdb -h localhost -U televault <name>
age -d -i televault-backup-key.txt archive_<name>.dump.age | pg_restore --no-owner --no-privileges -h localhost -U televault -d <name>

# 3. with the ORIGINAL FERNET_KEY in .env, give each archive its login back (the per-archive Postgres roles do not travel inside a database dump)
python scripts/secure_archives.py --repair
python scripts/migrate_archives.py --dry-run        # every archive should report "already up to date"
```

Then restart the API, archiver and backfill workers. `--no-owner` is used because the original owners (the per-archive roles) do not exist on a fresh server; step 3 creates fresh ones and stores their new passwords.

**What was rehearsed, and what was not.** A full cycle (create control + archive databases the production way, back up, verify, delete the databases *and* the archive role, restore as above, run step 3, read a chat back out of the restored archive) was run once in a Linux sandbox with PostgreSQL 16. **Not** tried: Windows, `--docker-service` and a restore through `docker compose exec` (the same commands with `docker compose exec -T postgres` in front of `pg_restore` and `createdb` should work, unverified), a restore onto a newer PostgreSQL major version, and a restore with a lost or changed `FERNET_KEY` (not possible by design: every stored Telegram session would stay unreadable and each account would have to log in again).
Rehearse it yourself on your own setup before you rely on it.

---

## 6. Routine checks

- Docker disk use: `docker system df`, then `docker system prune` occasionally. Never pass `--volumes`; that is what would delete the database volume.
- Disk space (`df -h`): archives only grow.
- After pulling updates: `pip install -r requirements.txt -c constraints.txt` (exact tested versions), then `alembic -c alembic_control.ini upgrade head`. Existing archive databases are migrated per account, not by that command (see "Updating existing archives" in section 2).
- Legal pages: while `instance/legal/` is missing, the server logs a warning at startup and serves the placeholder templates.