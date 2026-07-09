# Changelog

All notable changes to TeleVault will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned — Phase 3 (Advanced Features)

- Full-text search via SQLite FTS5 virtual table
- Backfill: archive historical messages sent before TeleVault was running
- Chat filter: allowlist/blocklist to control which chats are archived
- Storage mode: `all` (default) vs `deletions_only`
- Display name overrides: map a friendly label to a sender ID
- Username change tracking (`username_history` table)
- Auto-read policies: mark selected chats as read on a schedule
- Data management: clear all data, clear per-chat data (with irreversibility warning)
- Message ignore rules: filter by chat or text pattern before archiving
- TTL / retention policy: auto-delete archived messages older than N months
- Migrate `web/js/*.js` to ES modules — currently all classic scripts sharing
  one global lexical scope, which caused a real page-breaking `const`
  collision during Phase 2 (see [1.1.0]'s Fixed section); ES modules make
  this class of bug structurally impossible instead of a naming-discipline
  concern
- Saved Messages actor inference: `deleted_by_inference = 'self'` when a
  message's `chat_id` equals the archiving account's own Telegram user ID
  (only that one chat is deterministic this way — see [1.1.0]'s note on why
  private/group chats generally aren't). Needs the account's own ID
  captured once at startup and stored somewhere `flag_deleted()` can reach
  it — not done yet, `main.py` currently only uses `get_me()` for a log line
- Expose `SenderOut.resolved_name` as a computed API field instead of the
  same priority logic being duplicated in three places (the backend
  property, `messages.js`, `deleted.js`)
- Preserve a Deleted-view row's expanded detail panel across pagination and
  language switches (currently collapses — a deliberate trade-off, not a bug)

### Planned — Phase 4 (Infrastructure & Expansion)

- Media archiving (photos, documents, voice notes) with disk management
- PostgreSQL migration (swap SQLite for PostgreSQL + SQLAlchemy + Alembic)
- Orphan cleanup scheduler: reconcile DB records vs files on disk
- Notifications: Web Push via service worker (requires HTTPS)
- Read receipt inference: track `read_inbox_max_id` per chat (private chats only)
- Reactions tracking (`message_reactions` snapshot table) — low priority

---

## [1.1.0] — 2026-07-09

### Summary

Phase 2 complete: a read-only REST API and a full vanilla-JS/HTML/CSS PWA
web UI, covering all five planned views (Chats, Messages, Deleted, Stats,
Health). Backward-compatible with Phase 1 — the userbot process is
unchanged; the API server is a separate process that only reads the
shared SQLite file.

### Added — Archiving (`handlers/`)

- Call service messages now stored with humanized text labels (e.g.
  `[Missed call]`, `[Voice call · N min]`) instead of `NULL`

### Added — API (`api/` package)

- FastAPI + Uvicorn REST API, mounted under `/api`, with the static web UI
  served from `/` by the same process (`api/server.py`)
- Read-only DB access via `api/dependencies.py`'s `get_db()` — opens SQLite
  in `mode=ro` URI mode so the driver hard-errors on any accidental write,
  since the userbot and API share one file
- Routes: `GET /api/chats`, `/api/chats/{id}`, `/api/chats/{id}/messages`,
  `/api/messages`, `/api/messages/{id}`, `/api/deleted`, `/api/stats`,
  `/api/health`
- `db/read_queries.py` — read-only query layer backing all routes above;
  returns plain dicts, no ORM
- Pydantic v2 response schemas (`api/schemas/`): `ChatOut`, `ChatSummary`,
  `MessageOut`, `MessageDetail`, `SenderOut`, `EditOut`, `DeletionOut`,
  `StatsOut`, `ChatStatRow`, `PaginatedResponse`, `HealthOut`
- `LOWER_UNICODE()` custom SQL function registered per-connection — SQLite's
  built-in `LOWER()` only folds ASCII case, so this was needed for
  case-insensitive search across non-Latin scripts (e.g. Cyrillic)

### Added — Web UI (`web/` package)

- App shell: nav-rail + content-pane layout, "archive paper" (light) /
  "vault steel" (dark) themes via CSS custom properties, EN/UK i18n
  persisted to `localStorage`, PWA manifest + service worker (deliberately
  excludes `/api/*` from the cache — this is private data, and stale cached
  results would be actively misleading, not just stale)
- **Chats** view: paginated list with per-chat message/deleted counts and a
  last-message preview
- **Messages** view: global feed with text search (debounced) and an
  edited-only filter; search matches highlighted inline via `<mark>`
- **Deleted** view: paginated feed of deleted messages with the same search;
  each row expands (lazy-fetched, cached) to show the deletion record
- **Stats** view: global totals as stat cards (with client-computed
  deleted/edited percentages) plus a per-chat breakdown table
- **Health** view: liveness checklist (DB readable, session exists, message
  count) with manual refresh — no polling
- Shared `web/js/lib/`: `dom.js` (`escapeHtml`, `highlightMatches`),
  `pagination.js` (render + wire-up) — extracted once a second view needed
  identical logic, rather than duplicated per view
- The "wax-seal" `seal-badge` signature element, reused for deleted-message
  counts

### Added — Deletion actor inference (channel-only)

- `deleted_by_inference` now actually computes something: `'channel_admin'`
  for messages deleted from a broadcast channel, since regular subscribers
  cannot delete channel posts — only admins can, making this a structural
  fact rather than a guess
- Deliberately **not** implemented for private/group/supergroup chats —
  Telegram allows any party to delete a message for everyone with no time
  limit and no record of who did it, so a sender_id/timing guess there
  would be closer to a coin flip than a signal. This was the original
  Phase 1 plan (see migration 001); reviewed and dropped during Phase 2 in
  favor of leaving those permanently `'unknown'`
- New migration `db/migrations/002_channel_admin_only_inference.py` —
  rebuilds `message_deletions`' CHECK constraint from
  `('self', 'other', 'unknown')` (never actually used — see Fixed below) to
  `('channel_admin', 'unknown')`

### Fixed

- **`chat`/`sender` always null in `/api/messages`, `/api/deleted`, and
  `/api/messages/{id}`** — the SQL correctly joined `senders`/`chats`, but
  nothing reshaped the flat, prefixed columns (`sender_id`, `chat_name`...)
  into the nested objects `MessageOut` expects, so Pydantic silently
  defaulted both to `null` on every row. Added `_shape_message_row()` in
  `db/read_queries.py`; `get_message_detail()` also gained a `chats` join
  it was missing entirely. Verified against a live SQLite DB, not just
  parse-checked
- **Docstrings describing deletion-actor inference as implemented when it
  wasn't** — migration 001's and `DeletionOut`'s docstrings both described
  a "best-effort guess based on sender_id and timing" that, on inspection,
  no code anywhere actually computed; every row got `'unknown'` purely from
  the column default. Corrected to describe the actual (and, since this
  release, final) channel-only design
- **Page-breaking `const` redeclaration** between `messages.js` and
  `deleted.js` (both declared an unprefixed `SEARCH_DEBOUNCE_MS`) — none of
  `web/js/*.js` use ES modules, so top-level `const`/`let` share one global
  lexical scope across every `<script>`-loaded file; the second file to
  load threw a `SyntaxError` at parse time, breaking the entire page.
  Renamed to per-view-prefixed constants; see Phase 3's planned ES-module
  migration above for the structural fix
- Favicon 404 — file was placed at the project root, which `uvicorn` never
  serves (only `web/` is mounted); moved to `web/favicon.ico`, added an
  explicit `<link rel="icon">` tag
- `seal-badge`'s signature tilt (`rotate(-1.5deg)`) misaligning rows in the
  Chats view — a fixed rotation angle displaces wider boxes higher than
  narrower ones, and badge width varies with the deleted-count text.
  Scoped `transform: none` override for that context only; the tilt is
  unchanged everywhere it's used in isolation
- Chats-view labels (deleted count, chat type, pagination) not re-translating
  on a language switch — they're built dynamically from fetched data, not
  marked `data-i18n`, so `applyTranslations()` had no way to reach them.
  `i18n.js` now dispatches a `televault:langchange` event after switching;
  views listen and re-render their cached data

### Changed

- `.chat-type-badge` renamed to `.info-badge` — needed as a generic neutral
  pill by a third, unrelated use (deletion actor-inference labels), not
  just chat type
- `.chat-pagination*` CSS classes and `chats.pageOf`/`prev`/`next`/`type.*`
  i18n keys renamed to `common.*` / `.pagination*` once the Messages view
  needed the identical logic

### Schema

```
message_deletions (id PK, message_id FK, text_snapshot, deleted_at,
                    deleted_by_inference, inference_confidence)
    deleted_by_inference CHECK IN ('channel_admin', 'unknown')  -- was
    ('self', 'other', 'unknown') in migration 001, never populated with
    those values; see migration 002
```

### Known Telegram protocol behaviours (documented)

- `updateDeleteChannelMessages` (the event type that carries a `chat_id`)
  fires for **both channels and supergroups**, not channels exclusively —
  important because they have different deletion permissions (channels:
  admins only; supergroups: members can typically delete their own
  messages). The channel-only actor inference above checks `chat_type`
  explicitly rather than assuming from which event type fired

---

[Unreleased]: https://github.com/Gvneshv/TeleVault/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/Gvneshv/TeleVault/compare/v1.0.0...v1.1.0

### Summary

Phase 1 complete. Real-time text message archiving across all chat types.
Deleted messages are flagged and preserved. Edit history is tracked.
Stable enough for always-on deployment.

### Added

- `main.py` — async entry point; graceful shutdown on Ctrl-C (all platforms) and SIGTERM (Unix)
- `config.py` — frozen `Settings` dataclass loaded from `.env` via `python-dotenv`
- `db/connection.py` — SQLite connection with WAL mode, FK enforcement, and ISO 8601 datetime adapter
- `db/schema.py` — DDL for `chats`, `senders`, `messages`, `message_edits`, `message_deletions` tables; applied on every startup
- `db/queries.py` — all read/write operations with explicit `commit` / `rollback` (no context-manager transactions)
- `db/__init__.py` — package interface re-exporting `init_db`, `get_connection`, `close_db`, `apply_schema`
- `handlers/on_message.py` — `NewMessage` handler; upserts chat and sender rows defensively before inserting the message
- `handlers/on_delete.py` — `MessageDeleted` handler; falls back to `tg_message_id`-only lookup for private/group chats where `chat_id` is absent
- `handlers/on_edit.py` — `MessageEdited` handler; skips recording when `old_text == new_text` (Telegram fires edits for non-text changes)
- `handlers/helpers.py` — shared utilities: `get_chat_type()`, `get_sender_fields()`
- `utils/logging_setup.py` — console + rotating file handler (5 MB × 3 backups); Telethon pinned to WARNING
- `.env.example` — documented environment variable template
- `.gitattributes` — enforces LF line endings on Windows checkouts
- `requirements.txt` — pinned dependencies: `telethon`, `python-dotenv`
- `README.md` — setup guide, smoke test instructions, project structure, Telegram ToS note

### Schema (v3)

```
chats          (chat_id PK, name, username, chat_type, first_seen)
senders        (sender_id PK, username, first_name, last_name, first_seen)
messages       (id PK, tg_message_id, chat_id FK, sender_id FK, text,
                date, is_edited, edited_at, is_deleted, deleted_at, archived_at)
               UNIQUE INDEX on (tg_message_id, chat_id)
message_edits  (id PK, message_id FK, old_text, new_text, edited_at)
message_deletions (id PK, message_id FK, text_snapshot, deleted_at)
```

### Known Telegram protocol behaviours (documented)

- `MessageDeleted` carries no `chat_id` in private/group chats — fallback searches by `tg_message_id` alone
- Scheduled/auto-posted messages bypass `NewMessage`; arrive only as edit events — handled by defensive upsert
- Anonymous admin posts use the group's own (negative) ID as `sender_id` — stored as-is
- `MessageEdited` fires for link preview attachment, keyboard updates, view count increments — skipped when text is unchanged

### Technical decisions

- All datetimes stored as local-timezone ISO 8601 strings without milliseconds
- `detect_types` removed from SQLite connection — converter does not fire reliably in Python 3.14 module context
- `with conn:` (context manager) replaced with explicit `commit` / `rollback` — Python 3.12+ changed context manager semantics, breaking FK visibility across sequential writes
- Each DDL statement committed individually to avoid FK constraint failures under WAL snapshot isolation
- `upsert_chat` / `upsert_sender` accept `commit=False` to allow grouping all inserts into one transaction
- `loop.add_signal_handler(SIGTERM)` wrapped in `try/except NotImplementedError` for Windows compatibility

---

[Unreleased]: https://github.com/Gvneshv/TeleVault/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/Gvneshv/TeleVault/releases/tag/v1.0.0