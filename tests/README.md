# TeleVault automated tests

```bash
pip install -r requirements-dev.txt
python -m pytest                 # whole suite: ~5 min on a 2-core cloud VM (1149 tests), 10 min on an HDD machine - see "How long it takes"
python -m pytest tests/unit      # no database needed, a few seconds
python -m pytest -k leakage      # one topic
```

## What it needs

* **Unit tests** (`tests/unit/`) need nothing but Python.
* **Integration and API tests** (`tests/integration/`, `tests/api/`) need a real PostgreSQL server, because the code
  relies on things no fake provides (`pg_trgm`, triggers, a partial unique index, read-only sessions). By default they use
  the docker-compose Postgres on `localhost:5432` (`docker compose up -d postgres`). Point at another server with
  `TELEVAULT_TEST_SERVER_URL=postgresql+psycopg://user:pass@host:5432/postgres`; the role needs `CREATEDB`.
* With no reachable server the database tests are **skipped** with a message. Set `TELEVAULT_TEST_REQUIRE_DB=1` (e.g. in CI)
  to turn that into a failure, so a missing database can't silently look like a green run.

## How long it takes
The whole suite takes **minutes, not one minute**: about 5 minutes (1149 tests, 290 s) on a 2-core cloud VM, and about 10 minutes (1059 tests, 593 s) reported on a machine with a hard disk.
Most of it is not the tests themselves but their setup: each API test provisions real accounts and a real archive database (about 1 s each), and creating a database is bound by disk syncs, so an SSD should help a lot on a spinning disk; the exact gain has not been measured.
Use `python -m pytest tests/unit` (seconds) or `-k <topic>` while working, and run the whole suite before delivering a change.

## Why it is safe to run next to real data

`tests/conftest.py` explains this in full. In short: fresh secrets and a temporary `DATA_DIR` are set before `config.py`
is imported (your `.env` cannot leak in); every database is named `televault_test_<run>_...` and the drop helper refuses any
other name; opening a real Telegram connection or launching `main.py` / `backfill.py` raises instead of happening.
Leftover test databases from a killed run are harmless and can be dropped by name prefix.

## Layout

| Path | What it checks |
|---|---|
| `unit/test_security.py`, `test_crypto.py` | password hashing, JWT handling, Fernet round-trips and tamper/wrong-key behaviour |
| `unit/test_config.py` | required settings, defaults, `.env` resolution |
| `unit/test_event_handlers.py`, `test_handlers_helpers.py` | Telethon handlers with fake events: media, edits, deletions, privacy of what gets stored |
| `unit/test_worker_guards.py`, `test_runtime_files.py`, `test_atomic_write.py` | heartbeat / backfill status files, stale-file self-healing |
| `unit/test_manage_admin_helpers.py`, `test_read_queries_pure.py`, `test_telethon_compat.py` | CLI helpers, query building, typing shims |
| `integration/test_archive_db.py` | the real archive schema: upserts, deletion flagging, search, read-only role |
| `integration/test_startup_guard.py` | the first-admin startup guard |
| `integration/test_ingest_blocklist_db.py` | the ingest blocklist's table and migration (incl. downgrade round trip), queries, how a rule follows a group's upgrade to a supergroup, and an archive that has not been migrated yet |
| `integration/test_backfill_blocklist.py` | the real `backfill.run()` on a real archive with a fake Telegram: blocked chats are never fetched or stored, also when named explicitly |
| `integration/test_archive_login_isolation.py` | each archive's own Postgres role: it opens only its archive (not another's, not the control DB), has no extra power; securing an existing archive; `scripts/secure_archives.py`; a least-privileged server login |
| `integration/test_account_archive_login.py` | a worker gets its archive's own login from the account row, and refuses to start on an unusable one |
| `unit/test_archive_login.py` | decrypting the stored archive login: none, complete, half-written, undecryptable - and never printing the password |
| `api/test_archive_login_api.py` | provisioning stores the encrypted login; requests authenticate as the archive's role; a broken login is a 503, not a fall-back; deleting an account drops its role |
| `integration/test_migrate_archives.py` | `scripts/migrate_archives.py`: dry run, migrate and repeat, one broken archive does not stop the rest, no URL or password in output |
| `unit/test_ingest_filter.py` | the blocklist's failure policy: unreadable rules mean "skip", never "archive"; which messages an ignore rule may look at (plain text only, never a caption or a service message) and the same failure policy for the ignore rules |
| `unit/test_on_read.py` | the owner's Telegram reading position (handlers/on_read.py): real Telethon updates through the real builder - only inbox reads are listened to, a played voice note is not a position, blocked chats are skipped, the start-up sync, database outages are logged briefly, cancellation still works |
| `integration/test_unread_count_db.py` | the chat list's unread badge (`unread_count`): the `idx_messages_chat_tg_id` migration (chain, round trip, column order in the migration and in `db/schema.py`), what is counted (after the position, own messages left out, deleted and channel posts counted, chats kept apart), the cap, null versus zero, one statement per page, an un-migrated archive |
| `integration/test_read_state_db.py` | the `chats.read_inbox_max_id` column: migration round trip, a position only moves forward, no chat is created from a read report, an upgraded group's old id is not written onto the supergroup, an un-migrated archive (quiet, one warning, migrated while running) |
| `unit/test_text_matching.py` | how an ignore rule matches: substring, ignoring case, Unicode-normalized, spaces and word endings significant, no regex |
| `integration/test_ignore_rules_db.py` | the ignore-rule tables and migration (incl. downgrade round trip and drift check), which rules apply to which chat, how a scoped rule follows a group's upgrade to a supergroup, a rule outliving its chat, and an archive that has not been migrated yet |
| `integration/test_backfill_ignore_rules.py` | the real `backfill.run()` with ignore rules: a matching message is not stored, a caption is not matched, unreadable rules fail the chat instead of archiving it, nothing is logged |
| `api/test_ingest_ignore_rules.py` | ignore-rule API: add / list / remove, limits and 422s, duplicate phrase, unknown chat, per-account isolation, the read-only/write connection split, an unmigrated archive, nothing logged |
| `api/test_ingest_blocklist.py` | block / unblock API: idempotency, limits and cap, per-account isolation, the read-only/write connection split, an unmigrated archive |
| `api/test_auth_flow.py` | register / login / throttling / refresh rotation and reuse detection / logout |
| `api/test_gate_chain.py` | every endpoint behind the right gate, with the right status and reason code; the "no archive yet" message is about the database, not about linking Telegram |
| `api/test_tenant_isolation.py` | one account can never read, change or infer another account's archive |
| `api/test_secret_leakage.py` | no credential, session, hash or token appears in any GET response, the audit log or the token table; secrets are encrypted at rest |
| `api/test_admin_api.py` | invites, lock / unlock, deletion rules (including dropping the archive database) |
| `api/test_archive_provision.py` | per-account archive creation |
| `integration/test_logging_configuration.py` | the API server really configures logging (no caplog shortcut), and provisioning does not undo it |
| `api/test_telegram_sessions.py` | Telegram device list / end one / end all others / unlink ending the session, with a fake Telegram client: guards (linked, archiver stopped), own-session protection, Telegram refusals, nothing sensitive logged |
| `api/test_telegram_qr.py` | QR login with a fake Telegram client: start/poll/cancel, rotation, scan, 2FA, expiry, errors, the token never logged or cached |
| `api/test_telegram_send_code.py` | send-code with a fake Telegram client: the delivery type is logged, the phone number never is |
| `api/test_input_limits.py` | every client-supplied value is bounded; login stays lenient about format |
| `unit/test_static_mount_paths.py` | unknown `/api/` paths give a JSON 404 with either path separator (Windows regression) |
| `unit/test_docker_setup.py` | the compose file and init scripts agree with the app's default config |

## Conventions

* Endpoint lists come from `app.openapi()`, so a **new route is automatically included** in the "every route is gated"
  and "no GET leaks a secret" checks. A route that should be public must be added to the `PUBLIC` set in `test_gate_chain.py`
  on purpose.
* Read an archive's name from the API response (`archive_db_ref`); never recompute it - the tests give each one a unique suffix.
* `xfail(strict)` marks a **known bug** with its explanation. When the bug is fixed the test starts passing and strict
  mode fails the run until the marker is removed. There are none at the moment.
* A new bug fix should come with a test that fails when the fix is reverted. Do the revert once by hand to be sure.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull request: the full suite on Linux against a real
PostgreSQL 16 service container (with `TELEVAULT_TEST_REQUIRE_DB=1`, so a missing database fails instead of skipping),
`pyright` on the application code, and the database-free unit tests on Windows. Dependencies are installed with
`-c constraints.txt`, so CI and your machine use the same versions.

## Not covered

The real Telegram login and network behaviour, a live archiver observing a real deletion, the browser UI, the Windows
toggle scripts, and running the suite on Windows. See `TESTING.md` for the manual checklist.