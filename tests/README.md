# TeleVault automated tests

```bash
pip install -r requirements-dev.txt
python -m pytest                 # whole suite (~1 min)
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
| `api/test_auth_flow.py` | register / login / throttling / refresh rotation and reuse detection / logout |
| `api/test_gate_chain.py` | every endpoint behind the right gate, with the right status and reason code |
| `api/test_tenant_isolation.py` | one account can never read, change or infer another account's archive |
| `api/test_secret_leakage.py` | no credential, session, hash or token appears in any GET response, the audit log or the token table; secrets are encrypted at rest |
| `api/test_admin_api.py` | invites, lock / unlock, deletion rules (including dropping the archive database) |
| `api/test_archive_provision.py` | per-account archive creation |
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