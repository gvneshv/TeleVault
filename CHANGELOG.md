# Changelog

All notable changes to TeleVault will be documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed - navigation rail: icons, collapsing, one-button language (chat view, step B)
Second step of the chat view (`claude/chat-view-spec.md`): the shell around the future chat panel. The pages themselves are unchanged.
- **Every tab has an icon** and a text label (`web/js/lib/icons.js`, inline SVG - no CDN, no icon font). Chats, Messages, Deleted, Stats and Health use the paths drawn in the approved mockup
  (the owner asked to keep the stats and health ones); Settings, Backfill, Admin, the archiver states, log out, language and collapse/expand are Lucide icons, first candidate of each shortlist
  (cog, rotate-ccw-clock, user-star, package-x / package-plus, log-out, earth, fold-horizontal / unfold-horizontal). Lucide is ISC-licensed and the icons it took from Feather (log-out is one) are MIT-licensed:
  both notices are kept in `web/icons/LICENSE-lucide.txt`. The theme button shows Lucide `sun` (light theme) or `moon` (dark theme): both SVGs sit in the button and CSS shows the one matching `<html data-theme>`,
  so there is no flash and no script swap; the other pages keep the text glyph.
- **The rail collapses to a 64px icon strip and back** (button beside the wordmark; the choice is remembered per browser in `localStorage`, which may be unavailable - then it simply starts expanded).
  In the collapsed rail every label is *visually* hidden, not removed, so each button keeps its accessible name, and each tab shows its name as a tooltip in the app language.
  The collapse button names the action ("Collapse the sidebar" / "Expand the sidebar", `aria-expanded` carries the state).
- **Bottom controls**: the archiver button (state icon + the existing dot/ring + label; in the collapsed rail the dot moves onto the icon and the label becomes the tooltip), then log out, theme and language as icon buttons.
  **Language is now one button** that switches to the next language (shows `EN` / `UK`; its label names the current and the next language). The other pages keep their `<select>`; `i18n.js` supports both.
- **Phones**: the rail is a two-row top bar (brand, username and the four controls on top, all tabs as icons below); no collapse button there. New strings in `en.js` and `uk.js`; web shell cache v42.
- Messages and Deleted stay in the rail for now - they are the old pages and go when the new chat view replaces them (step C).
- Checked in a real browser (Chromium, mocked API, throwaway script - the repo still has no browser tests): collapse/expand, persistence across a reload, language and theme switching, every tab opening, accessible names while collapsed, archiver icon per state,
  the non-admin DOM, no horizontal scroll, no console errors, in dark and light and at 390px. Not checked: Safari/Firefox, a real touch device, a screen reader.

### Added - chat view backend (step A) and "delete everything stored for a chat"
First step of the chat view (the design is in `claude/chat-view-spec.md`); nothing in the web UI changes yet - these are the API pieces the new panel will stand on.
- **`edit_count` on every message row** (feed, per-chat list, detail) and **`edited_count` on chats**, so a list can say "Edited 3 times" without one request per message. The per-message
  count is a correlated count over `idx_message_edits_message_id`; a page is at most 200 rows. `edited_count` already existed as a trigger-maintained column - it is now simply exposed.
- **`GET /api/chats/{chat_id}/window`** - one chat's conversation as a window addressed by message instead of page number: no anchor = the newest messages, `around=<message id>`,
  `around_date=<ISO 8601>`, `before=<id>` (scroll up), `after=<id>` (scroll down). Rows come oldest-first with `has_older` / `has_newer` / `anchor_id`. It is keyset pagination on `(date, id)`:
  the id breaks ties between messages with an identical timestamp (a backfill stores whole bursts of them), and unlike `OFFSET` it stays correct when rows appear between two requests.
  At most one anchor (422 `too_many_anchors`), every number bounded (limit 1-200, ids 1..2^63-1, 422 not a Postgres overflow), a bad date is 422 `invalid_date` (no offset = UTC).
  A message id from another chat or another account is indistinguishable from a missing one (404 `message_not_found`). Read-only connection. The old page-number endpoints stay until the panel replaces them.
- **`DELETE /api/chats/{chat_id}`** - removes everything stored for a chat, **irreversibly**, in one transaction: messages, edit history, deletion snapshots (their text goes too), the `chats` row,
  group-migration records that mention it, backfill runs aimed only at that chat (by id or `@username`; a run over every chat stays), and every sender who appears in no other chat.
  Meant for chats whose Telegram account no longer exists (they show as a bare number and clutter every picker).
  - **An ingest-blocklist rule is kept by default** (`block_rule_kept` in the response) - the rule has no foreign key to `chats` for exactly this reason; "forget and keep ignoring" must not quietly stop ignoring.
    **`?also_unblock=true` removes it in the same transaction** - the owner's explicit choice at purge time (the confirmation dialog will offer it as a tick box, default off):
    forget the chat AND archive it again if it writes to you. Only that chat's rule goes, and a failed purge leaves the rule in place (never "unblocked but not purged").
  - **The archiver may keep running.** The chat row is locked first, so a message being written at that moment waits for the purge and is removed with the rest. If the chat receives a new message
    afterwards it is created again, empty, from that message on. One message already past its chat upsert and waiting on the lock can fail its foreign key and is lost (a few-millisecond window per purge).
  - **Refused while this account's backfill runs** (409 `backfill_running`): it could store the removed history again. A stale status file from a crashed backfill does not block it.
  - Only the caller's own archive is reachable; another account's chat id is "not found". It is the one write route in `api/routes/chats.py`; the test suite pins that no GET uses the write connection.
  - Not done yet: the button and confirmation dialog (they arrive with the chat view), and automatic detection of deleted Telegram accounts.
- Tests: 100+ new ones in `tests/integration/test_chat_view_queries.py` and `tests/api/test_chat_view_api.py`, including a real two-connection test of the lock and a rollback test using a trigger that makes the last step fail.
  Each rule was broken on purpose once (no id tie-break, no chat filter on the anchor, a shared sender deleted, the block rule removed, no backfill guard, no row lock) and a test went red every time.

### Security - every archive has its own Postgres login
Until now all archives were reached with the one login in `CONTROL_DATABASE_URL`, so Postgres could not stop a connection meant for one account from opening another's archive - only the
application's care kept them apart. The isolation tests showed the application itself never mixes them up (each request resolves the caller's own archive from their account row), but
nothing below the application enforced it. Now:
- **Provisioning creates a role per archive** (`db/provisioning.py`): named like the database, `LOGIN` only (no superuser, `CREATEDB`, `CREATEROLE`, replication), the owner of its one
  database, with a random password. `CONNECT` is revoked from `PUBLIC` on the archive **and on the control database** (password hashes, Telegram sessions), so no archive role can open anything but its own archive.
  The password is stored Fernet-encrypted in new columns `users.archive_db_user` / `users.archive_db_password` (control migration `c4a1f7d29b58`), never logged, never in a response, never on a command line;
  the one statement that carries it runs on the raw psycopg connection because SQLAlchemy would copy it into an exception message.
- The connection layer takes the login (`db.ArchiveLogin`, password hidden from `repr`): the API (`api/dependencies.py`, health, backfill cancel) and both workers (`utils/account.py`) open
  each archive as its own role. Pools are per (database, role). **An unusable stored login is a 503 / a worker that refuses to start - never a quiet fall-back to the server login.**
- **Existing archives**: `python scripts/secure_archives.py` (`--dry-run` first) gives each one a role, hands it the tables, proves the login works and only then stores it; `--repair` replaces a stored
  login that no longer works (e.g. a lost `FERNET_KEY`). Until it is run they keep working with the server login. Restart the workers afterwards. `scripts/migrate_archives.py` migrates each archive as its own role.
- Deleting an account drops its role with its database. `set-archive` (manual attach) clears any stored login. The server login now needs `CREATEROLE` as well as `CREATEDB` (tested with a non-superuser that has exactly those).
- Tests: a role cannot connect to another archive or the control database and has no extra power; securing an existing archive keeps its data and lets the role migrate and write (trigger functions included);
  requests and workers authenticate as the right role; a broken login fails closed; the script's dry run / repeat / failure handling; least-privileged server login. Each safeguard was broken on purpose once to see a test catch it.
- `provision_archive_database()` now returns a `ProvisionedArchive(db_name, login)` instead of the bare name. See RUNBOOK.md, "Who can open an archive".

### Added - ingestion-time chat blocklist ("Chats not archived")

- **Settings -> Chats not archived** lists the chats TeleVault must not archive and lets you add or remove one. A chat on the list gets no new messages and no edits stored.
  Everything else is archived as before - **a new chat is always allowed until you block it**, so there is no allowlist to maintain. This is a different thing from the
  display-time chat filter on the Messages/Deleted views (shipped in [2.1.0]), which only changes what is *shown* from what is already archived.
- **The decision is made before anything is read or written.** The live handlers (`handlers/on_message.py`, `handlers/on_edit.py`) ask `handlers/ingest_filter.py` right after
  they have the chat id: before the message text is resolved, before `get_chat()`/`get_sender()` (which can cost Telegram API calls), before any database write.
  `backfill.py` applies the same rule per chat, before a single message of it is requested - also when the chat is named with `--chat`: the run says so and archives nothing
  instead of quietly overriding your rule.
- **Edits are filtered too, on purpose.** The edit handler stores the new text of a known message and *inserts* a message it has never seen, so filtering only new messages would have
  let a blocked chat keep leaking through edits.
- **Takes effect on the next message, no restart.** The rule is one primary-key lookup per event in the account's own archive, not a copy held in memory. A short-lived
  in-memory cache was considered and left out: the live handler is low-volume and already does several round-trips per archived message, and a lookup has no refresh interval, no stale copy that can
  disagree with the Settings page, and no invalidation to get wrong. If an account's traffic ever makes it show up in a profile, a cache is a small change inside `should_skip()`.
- **When the rules cannot be read, the event is skipped, not archived.** A lost message is the ordinary cost of the database being down (the insert would have failed too); guessing
  "not blocked" could store a chat you asked never to store. The one exception is an archive without the table (below).
- **Blocking only stops FUTURE archiving.** Messages already archived stay and remain visible; removing them is a separate destructive action: `DELETE /api/chats/{chat_id}` (see "chat view backend" above).
  Deletion events are deliberately *not* filtered: a deletion adds no content, only flags a row already stored, and for private chats and legacy groups Telegram does not say which
  chat it came from. What arrives while a chat is blocked is never stored and cannot be recovered by unblocking.
- **A blocked group stays blocked when it is upgraded to a supergroup.** The upgrade gives the chat a new id; `record_chat_migration()` now *moves* the rule to the new id, and the
  lookup canonicalizes through `chat_migrations` first, so "is this blocked?" and "which chat would this be stored under?" cannot disagree. The migration service message itself is
  still processed for a blocked chat (it holds no text; it is what carries the rule over).
- **A rule has no foreign key to `chats`** and keeps its own copy of the chat's name, so "delete everything stored for this chat" can leave the block (and a readable list
  entry) in place instead of silently lifting it.
- API: `GET /api/ingest/blocklist`, `PUT` and `DELETE /api/ingest/blocklist/{chat_id}` (both idempotent). Only chats the archive already knows can be blocked (404 `chat_not_found`),
  so the client never sends free text. Bounded: the chat id must fit 64 bits (422 otherwise, not a database overflow error), at most 2000 rules per account (409 `blocklist_full`).
  Both limits live in `utils/validation.py`. Each account sees and changes only its own list; being the admin changes nothing.
- **Existing archives need one migration** (revision `0e528a688c4c`): run `python scripts/migrate_archives.py` once (`--dry-run` first to only see which archives are out of date;
  RUNBOOK.md, "Updating existing archives"). Nothing applies it automatically. Until it is applied nothing breaks: the workers treat the missing table as "no rules" (they log one warning) and
  archive as before, and the blocklist endpoints answer 409 `archive_outdated`, which the Settings card shows in plain words. Archives created from now on already have it.
- **`scripts/migrate_archives.py`** migrates every archive in one go. The manual `alembic -x url=...` needed a password nobody has: archives have no password of their own, they all
  share the app's role from `CONTROL_DATABASE_URL`, which the script reads from `.env`. It prints no URL or password, is safe to repeat and to run while the app runs, keeps going
  past an archive that fails (exit status 1), and has a `--dry-run`. Supporting pieces: `control_db.queries.list_archive_databases()` (reads no secrets) and
  `db.provisioning.migrate_archive_database()` / `head_revision()` / `archive_revision()`.
- The API gained its second archive write path, `get_archive_write_connection()` (the first is `cancel_backfill`). Routes that only read keep the read-only connection, and a test pins
  which routes use which, so a read route cannot quietly gain write access.
- New error reasons, translated in both languages: `archive_outdated`, `blocklist_full`, `chat_not_found`. Web shell cache v40.
- Tests: the migration (including a downgrade/upgrade round trip), the queries, the group-upgrade rule move, the unmigrated-archive behaviour, both handlers (a blocked chat's content is
  never read and Telegram is never asked about it), the failure policy, a full `backfill.run()` against a real archive with a fake Telegram, and the API (limits, cap, isolation, outdated
  archive). Each rule was shown to fail when its code was reverted once. Not covered by automated tests: real Telegram events and the UI in a real browser - see TESTING.md.
- Not done, by design: allowlist mode (it needs "seen but not archived" metadata about chats you chose not to store), blocking from inside a chat's own view, and
  per-chat data removal (the backend shipped afterwards, see above; its button comes with the chat view).

### Changed - roadmap and legal texts brought up to date

- Removed three stale items from "Planned - Phase 3". They stayed listed only because nobody removed them when the work shipped:
  the **admin API endpoints** (invite creation, listing/locking users; in use as `/api/admin/*` and the Admin tab), the **Saved Messages actor inference**
  (shipped: `flag_deleted()` sets `deleted_by_inference = 'self'` for the account's own Saved Messages chat, and the Deleted view shows it; it is the one chat where the actor is certain,
  every other private/group chat stays `unknown` because a deletion event carries no actor), and the **Deleted-view expanded-panel** item (already implemented).
- Privacy Policy and Terms: describe the new Telegram devices card (what is fetched, that it is shown but not stored or logged, that nothing is ended without confirmation)
  and that unlinking now ends the session in Telegram. Both documents move to version `2026-10-22` (`CURRENT_TERMS_VERSION`); the wording change is material because it covers a
  new kind of action, as the policy's own section 12 promises.

### Changed - account linking page layout

- The three blocks on the linking page (the "all set" banner, the Telegram card, the archive card) are now the same width, 560px instead of up to 420px, and the QR code is centered in its card.

### Changed - relink Cancel is now a bar above the step

- While relinking, a bar at the top of the Telegram card says "You are relinking your Telegram account." and carries a filled, danger-styled Cancel button on every step
  (QR, phone, code, password, credentials). The old Cancel sat among the form buttons and read as one more option. Cancelling from the QR step also tells the server to drop the QR login.

### Added - end Telegram devices from Settings, and unlinking ends TeleVault's own session

- Every link (QR or code, first time or relink) creates a new session in the person's Telegram account, and nothing ever ended the old ones.
  **Settings -> Telegram devices** now lists them (the same list as Telegram's Settings -> Devices: device, app, system, place, IP, dates) and ends one
  device or all devices except TeleVault's own. TeleVault's own entry carries a badge and no button; ending it is "Unlink Telegram".
- **Unlink Telegram** now also logs TeleVault's session out inside Telegram (`auth.logOut`), so it no longer lingers in the device list. It stays best
  effort: if Telegram cannot be reached the account is still unlinked here and the response/UI says `ended_in_telegram: false`, telling the person to remove
  the entry in Telegram themselves. A session Telegram already ended counts as ended.
- New endpoints: `GET /api/telegram/sessions`, `DELETE /api/telegram/sessions/{id}`, `DELETE /api/telegram/sessions` (all others). `DELETE /api/telegram/session`
  (unlink) now returns `ended_in_telegram`. Logic lives in `utils/telegram_sessions.py`; routes in `api/routes/telegram_sessions.py`.
- **This is the first time the API process connects to Telegram with a stored session** (until now only the archiver and backfill workers did, and the API only
  linked new sessions). Hence the guards: the account must be linked, its archiver and any backfill must be stopped (409, the same rule unlink already had),
  and only one device operation per account runs at a time. A worker started in the few seconds between the check and the end of the request is an accepted race.
- Privacy: the list contains IP addresses and places of the person's own sessions. It is sent to that account only with `Cache-Control: no-store`,
  kept in browser memory only (never localStorage), and never logged (log lines carry the user id and a count).
- New error reasons, translated in both languages: `telegram_session_invalid` / `telegram_session_unreadable` (link again), `telegram_session_too_new`
  (Telegram refuses to end other devices while TeleVault's own session is young - Telegram documents about a day),
  `telegram_device_not_found`, `telegram_cannot_end_current`, `telegram_unreachable` (502). The "already running" reason, which read "It's already running." on
  the unlink button, now says to stop the archiver first wherever Telegram is involved.
- A corrupted saved session string used to be able to raise inside Telethon's `StringSession()`; the new code reports it (`telegram_session_unreadable`) instead of a 500.
- The link-step and relink notes no longer say TeleVault cannot remove old sessions; they point to the new card. Saving new API credentials still clears the stored
  session (so TeleVault can no longer end it): the relink credentials form now says to end it first. Web shell cache v37.
- Not covered by automated tests: the real Telegram calls. See TESTING.md for the manual checks.

## [3.0.0] - 2026-10-21

### Summary

Multi-user TeleVault. Every account now has its own Telegram link (QR code or login code), its own archive database, and its own archiver and
backfill workers, all managed from the web UI; Telegram credentials no longer live in `.env`. **Breaking:** the `.env` variables and worker command lines
changed (see "per-account workers" below), and new usernames must match a stricter format (existing accounts keep working). Upgrading from 2.1.0:
update `.env` (a fresh clone needs only `CONTROL_DATABASE_URL`, `FERNET_KEY`, `JWT_SECRET`), run `pip install -r requirements.txt` (new `qrcode`
dependency), run `alembic -c alembic_control.ini upgrade head`, and relink Telegram in the web UI. Nothing migrates existing archive databases automatically.

### Changed - relinking starts with the QR code and can be cancelled

- "Relink a different account" now opens the same QR-first step as a first-time link instead of the API id/hash form; the credentials form is still
  reachable as a fallback ("Use different credentials"). A **Cancel - keep the current link** button is shown on the way, and the current link stays
  untouched until a new one succeeds. Saving new API credentials is the one step that signs the account out (the server clears the saved session),
  so that form says so first.
- The "linked", relink and unlink texts now say that none of this ends the session in Telegram itself: it stays under Settings -> Devices until it is
  terminated there. Web shell cache v36.

### Fixed - backfill progress bar could not be read

- The bar's empty track used `--color-border`, which is identical to the fill colour in the dark theme and nearly so in the light one, so progress was invisible.
  New token `--color-track` (`#0f172a` dark, `#e7e5fe` light) plus a thin outline so the track also shows against the light page. Web shell cache v35.

### Added - warning about leftover Telegram devices

- The link step now says that every link (QR or code, first time or relink, same computer or not) adds a new entry under Telegram's Settings -> Devices,
  and that TeleVault cannot remove the old ones. TeleVault only stores the session; it never logs one out of Telegram. Ending sessions from inside
  the app is planned separately.

### Changed - QR code is the primary way to link Telegram

- On the Telegram setup page the link step now leads with the QR code (a filled "Link with a QR code" button and a one-line explanation);
  the phone number + login code form sits below an "or" divider with a plain "Send code" button. Nothing about the code flow changed.
  The QR is still shown only when the button is pressed, not automatically: starting one opens a Telegram connection on the server for up to
  5 minutes, which should be a deliberate act (and must not restart by itself after an expired or failed attempt).
- An error from either method now appears at the top of the step instead of between the form and its buttons.

### Fixed - "archive hasn't been set up yet" blamed the wrong step

- With Telegram already linked but no archive database yet, starting the archiver or a backfill (and opening Chats, Messages, Deleted, Stats or
  Health) said "Finish linking your Telegram account to start archiving", sending people back to a step they had completed. The message now says
  "Your archive database hasn't been created yet." (English and Ukrainian) and offers a **Create archive database** link to the setup page - inline
  on the tabs and in the alert dialog the archiver button and the backfill start show. Only this one error gets a link; transient or
  admin-only errors (`archive_unavailable`, `archive_misconfigured`) stay plain text.
- The API's English `message` for `archive_unattached` changed the same way (`reason` and the 409 status are unchanged, so clients keyed on those are not
  affected). The "Go to settings" string is gone from both languages (it had no other use).
- `lib/dialog.js`'s `showAlert()` takes an optional `link: { href, label }` for this.

### Fixed - the completion banner touched the Telegram card

- The green "all set" banner on the setup page had no bottom margin, so it sat flush against the "Connect your Telegram account" card. It now has the
  same 20 px gap the two cards have between them.

### Changed - health marks and the archiver button's dot

- A passing health-check row now carries a green **✓** badge (accessible name "Yes" / "Так") like the red **No** badge failing rows already had,
  instead of an empty gap that made the rows look broken.
- New design token `--color-ok` (green; contrast 5.4:1 light, 6.8:1 dark) used for those marks and for the archiver button: **running** is a filled
  green dot, **stopped** a hollow grey ring (they differ in shape as well as colour). Before, both were a filled grey-teal that was nearly the same
  colour in the dark theme.

### Fixed - tabs did not show new messages until the page was reloaded

- Chats, Messages, Deleted, Stats and Health each loaded once, on first open, and never again, so messages stored by the running archiver were
  invisible until a full reload (Health only changed when its Refresh button was pressed). Switching back to one of these tabs now re-fetches it
  quietly: the old content stays on screen (no "Loading..." flash), nothing is redrawn when the answer is identical (so rows expanded in Deleted stay open),
  and a failed request keeps what is shown instead of replacing it with an error. Filters, search text and the current page are kept.
  There is no timer; clicking the tab you are already on refreshes it too. New `lib/refresh.js`.
- Each load now ignores its response if a newer request has started since, so a slow older answer (an old search term, say) can no longer overwrite
  newer results - this race existed before and the quiet refresh would have made it easier to hit.
- Known gap, unchanged: the chat filter's list of chats is fetched once per page load, so a chat that first appears after that is missing from the
  filter dropdown until a reload.
- Web shell cache bumped to v34.

### Added - link Telegram with a QR code

- Settings -> Telegram setup has a way to link an account without a login code: "Link with a QR code" (now the primary method, see above). It matters because
  Telegram decides whether and how a code is delivered (for some accounts it never arrives). Scan the picture in Telegram on your phone
  (Settings -> Devices -> Link Desktop Device). Two-step verification is handled by the same password form as the code flow.
- New endpoints `POST /api/telegram/qr/start`, `POST /api/telegram/qr/poll`, `DELETE /api/telegram/qr`. The server waits for the scan in a background task
  (Telethon only notices a scan while a wait is running) and the page polls every 2 s; the picture refreshes itself as Telegram rotates the token (~30 s).
  An attempt lasts at most 5 minutes. Session saving is shared with the code flow (`_save_session_and_discard`).
- Privacy: the QR token is a login secret. It is rendered server-side to an SVG (no third-party QR script sees it), sent only to the account that asked,
  with `Cache-Control: no-store`, and never logged. The page warns to scan only codes generated on that page.
- New dependency `qrcode` (pure Python; SVG output needs no Pillow), pinned in `constraints.txt`. Run `pip install -r requirements.txt` after updating.
- Web shell cache bumped to v33.

### Added - input limits (breaking for registration: stricter usernames)

- New `utils/validation.py`: one place for every limit. Usernames are 3-32 characters of `A-Z a-z 0-9 . _ -`; passwords 8-128 characters;
  invite tokens, Telegram api_id / api_hash / phone / code / 2FA password, search text, date filters and the chat-id filter list are all capped.
  Violations are a 422, never a 500 (a password over 4096 characters used to crash registration and login with a 500).
- Format rules apply to **new** accounts only (web registration and `manage_admin.py create`). Login only caps lengths, so existing accounts
  with older names keep working. The HTML forms carry matching `maxlength` / `pattern` attributes.
- A 422 from any form now shows a generic localized "values are not valid" message instead of "Something went wrong".

### Fixed

- **Telegram credentials form always failed with 422:** the handler read the input fields after re-rendering the form (which empties them), so it sent
  empty strings. It now reads them first, like the phone and code forms already did.
- **Unknown `/api/` paths returned the HTML 404 page on Windows** instead of the JSON 404: Starlette passes `api\foo` there and the guard only compared `api/`.
- **Theme icon showed the sun after a hard reload in dark mode:** the icon was set before the button existed. It is now synced on `DOMContentLoaded`.
  (Service worker cache bumped to v30 so browsers pick up the changed files.)
- **Docker created a second, unused `televault` database.** The container now creates only `televault_control` (`POSTGRES_DB`), and the extra init script is gone.
  Existing volumes keep the old empty database; see RUNBOOK section 2 for how to drop it.
- **Chat renames were never recorded:** `upsert_chat` now updates a chat's name and @handle when they change (and never blanks them when a lookup fails).

### Changed - the code step says where Telegram put the login code

- `POST /api/telegram/link/send-code` now also returns `delivery` (`app`, `sms`, `call`, `email` or `other`; a coarse category only, nothing about the code).
  The code step shows a matching explanation instead of the generic "Telegram sent a code to <number>": for `app` it says the code is a message in the
  chat named "Telegram" inside an app where the account is logged in, and that it is not an SMS. Text in English and Ukrainian; service worker cache v32.

### Fixed - the API server never logged anything below WARNING

- `api/server.py` now calls `setup_logging()` at startup, like `main.py` and `backfill.py` always did. uvicorn configures only its own loggers, so every
  INFO line from `api.*`, `db.*` and `control_db.*` (including the send-code delivery line below) was silently dropped. `LOG_LEVEL` and `LOG_FILE` now apply
  to the API server too. Verified against a real uvicorn: no SQL statements or parameters appear at INFO.
- Archive provisioning (alembic run inside the API process) no longer replaces the root logger's handlers and level with `alembic.ini`'s
  (`WARNING` + its own handler); it passes `configure_logger=False`. The standalone `alembic` command still configures logging as before.

### Changed - health panel wording, localized username hint, login-code diagnostics

- The health panel marks a failed check with a red **No** badge and a passed check with nothing, instead of an identical-looking ✓ / ✗ pair
  (an ✗ next to "Telegram session found" read like "found" to someone who had never connected Telegram).
- The username format hint shown by the browser on a rejected registration now follows the app language (`data-i18n-title`); before, the browser's own
  half of the message was localized and ours was always English. Service worker cache bumped to v31.
- `POST /api/telegram/link/send-code` logs how Telegram chose to deliver the code (`delivery=SentCodeTypeApp` = an in-app message from the
  official "Telegram" chat, `...Sms`/`...Call` = phone network, `...SetUpEmailRequired` = Telegram wants an e-mail step this flow cannot do).
  The phone number and anything code-related are never logged. This is how to diagnose "the request succeeded but no code arrived".

### Added - dependency pinning and CI

- `constraints.txt`: exact tested versions of every dependency (install with `pip install -r requirements.txt -c constraints.txt`).
- `.github/workflows/ci.yml`: full suite + pyright on Linux with a PostgreSQL service, database-free unit tests on Windows.
- `pyrightconfig.json`: type-checks application code only (the tests pass fakes and deliberately wrong types).
- Every fix above has a test that fails without it.

### Added - automated test suite

- `tests/` with 621 tests (`pip install -r requirements-dev.txt && python -m pytest`), documented in `tests/README.md`.
  Covers auth and throttling, refresh-token rotation and reuse detection, the gate chain on every endpoint, tenant isolation
  between accounts, secret leakage (credentials, sessions, hashes and tokens in responses, logs and storage), admin and
  provisioning flows, the event handlers and the worker coordination files.
- The suite is hermetic: fresh secrets, a temporary `DATA_DIR`, uniquely named throwaway databases (the drop helper refuses
  any other name), and real Telegram connections / worker launches are blocked.
- `TESTING.md` rewritten as the manual checklist only; it still described a removed `promote` command and hand-inserted invites.

### Fixed - Alembic silenced application logging

- `alembic/env.py` and `alembic_control/env.py` called `fileConfig(...)` with its default `disable_existing_loggers=True`,
  so running a migration in-process (as archive provisioning does) disabled every logger created before it. Now passes
  `disable_existing_loggers=False`. Covered by a regression test.

### Changed - type checking

- Pyright reports 0 errors in the application code (was 33 across 12 files; the test files, which pass fakes and deliberately wrong types, are not type-clean). Telethon's loose annotations are bridged in the new
  `utils/telethon_compat.py`; behaviour is unchanged.

### Changed - first-run admin guard

- **The API server now refuses to start on an instance where nobody could ever become the admin:** no admin account exists
  *and* `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` is unset. Previously that combination started fine and then rejected every
  registration with no hint why. The error names both fixes (set the token, or run `scripts/manage_admin.py create`).
  The token is still not needed once an admin exists. If the control database can't be reached (or isn't migrated), the
  check is skipped with a warning instead of blocking startup.

### Changed - per-account workers (breaking: `.env` and command lines)

- **Telegram credentials no longer live in `.env`.** `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, `SESSION_NAME`,
  `DATABASE_URL` and `DB_PATH` are gone. A fresh clone now needs only `CONTROL_DATABASE_URL`, `FERNET_KEY`,
  `JWT_SECRET` (+ optionally `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN`); `alembic -c alembic_control.ini upgrade head`
  works with exactly those. Every account links its own Telegram in the web UI, stored encrypted in the control DB.
  Previously `config.py` demanded the Telegram values at import time, so even `alembic` refused to run.
- **`main.py` and `backfill.py` are per-account workers:** `python main.py --user-id N` /
  `python backfill.py --user-id N`. They read that account's decrypted credentials + archive database from the control
  DB (`utils/account.py`), connect with a `StringSession` and **never prompt** - a revoked/invalid session exits with a
  clear message (a backfill also records an `error` run) instead of hanging on a login prompt. Only the id is passed on
  the command line, never the session.
- **Heartbeat and backfill-status files are per account** (`data/users/<id>/`, new `DATA_DIR` setting, `utils/worker_paths.py`),
  so several accounts can archive at once and every "is something running?" check (start, unlink, delete account) answers
  for the right account only.
- `/api/telethon/*` and `/api/backfill/*` act on the calling account; **starting** requires finished setup
  (`require_archiving_ready`: 409 `telegram_not_linked` / `archive_unattached`). `session_exists` on `/api/health` now
  reflects the account's own saved Telegram session.
- The API process has no "primary" archive: `db.init_server()` + `db.server_database_url()` derive each account's
  database from `CONTROL_DATABASE_URL`; the server also derives its CREATE/DROP DATABASE connection from it.
- The archive Alembic env no longer imports `config.py`; target a database explicitly:
  `alembic -x url=postgresql+psycopg://USER:PASS@HOST:5432/DBNAME upgrade head` (provisioning does this itself).
- `scripts/migrate_sqlite_to_postgres.py` takes `--sqlite-path` and `--target-db`.
- `scripts/toggle_archiver.ps1/.bat` take `-UserId` (not exercised in CI - Windows only).
- Subprocesses are launched with absolute script paths and `cwd` = repo root, so the API can be started from any directory.

### Removed

- The "instance owner" concept: `require_instance_owner`, `db.get_primary_database_name`, `HealthOut.is_instance_owner`,
  the `not_instance_owner` error and the "session not applicable" health text. Service worker cache bumped to `v29`.

### Fixed

- Invites created through the web UI were audited as `invite_created_via_script`; they are now `invite_created`
  (the script still logs the `_via_script` variant). Existing audit rows are not rewritten.

### Added

- **Multi-user authentication (backend)** - `POST /auth/register` (invite-token based),
  `/auth/login`, `/auth/refresh`, `/auth/logout`, `GET /auth/me`. Backed by a new, separate
  control database (`control_db/`, `alembic_control/` - see `control_db/schema.py` for why
  account data lives in its own database rather than alongside the per-user archive schema)
  - Refresh tokens rotate on every use and record *why* a token was revoked
    (`refresh_tokens.revoked_reason`: `rotated` / `logout` / `reuse_detected` / `account_locked`).
    Redeeming an already-rotated or already-reuse-flagged token revokes every other active
    session for that account (a legitimate client always moves on to its replacement, so seeing
    an old one again is a theft signal) - but an ordinary logout is recognized as exactly that,
    and does *not* trigger the same account-wide revocation
  - Login throttling is keyed per-account (5 failed attempts / 15 minutes blocks further attempts
    on that account specifically) rather than per-IP, so one person mistyping their password on a
    shared network/VPN can never lock anyone else's account out. A narrower IP-keyed fallback
    only applies to repeated guesses against usernames that don't exist at all, where there's no
    real account for it to affect
- `scripts/manage_admin.py` - `create`/`promote` subcommands to bootstrap or grant the first
  admin account outside the invite flow (which otherwise has no way to create user #1, since
  every invite must itself be created by an existing user)
- **Archive access is now gated to its owning account** - `require_owner` (`api/dependencies.py`)
  restricts `/api/chats`, `/messages`, `/deleted`, `/stats`, `/telethon/*`, and `/backfill/*` to
  the single account named by the new required `OWNER_USER_ID` setting. Deliberately NOT "any
  authenticated user" and NOT "any admin" - this archive is one person's private message
  history, and admin status (account management) is kept fully independent of archive access, so
  promoting a second admin (`manage_admin.py promote`) never grants them your messages. `/auth/*`
  and `/health` remain open, since they're what let an account prove who it is in the first
  place. `OWNER_USER_ID` has no default; the app refuses to start without it set correctly
  (see `.env.example`)
  - **Known gap this introduced, now closed (see login/register UI entry below):** the frontend
    initially had no login page or token storage, so Chats/Messages/Deleted/Stats/Backfill would
    have shown errors - `apiFetch()` (`web/js/lib/auth.js`) never attached the `Authorization`
    header `require_owner` needs
- Login/register UI (`web/login.html`, `web/register.html`, `web/js/views/{login,register}.js`) -
  closes the gap noted above: `web/js/lib/auth.js`'s `apiFetch()` now holds the access token in
  memory and attaches `Authorization: Bearer <token>` to every request, silently refreshing via
  the httpOnly refresh-token cookie first if none is held yet, and redirecting to `/login.html`
  only if that refresh itself fails
- Telegram/archive setup page (`web/telegram-setup.html`, `web/js/views/telegram-setup.js`) -
  standalone page (same pattern as `login.html`/`register.html`, not an in-SPA tab) driving the
  existing Telegram-linking flow (`POST /telegram/credentials` -> `/telegram/link/send-code` ->
  `/telegram/link/confirm`, including the 2FA password fork) and archive provisioning
  (`POST /archive/provision`), gated on the new `GET /telegram/status` endpoint below so a
  returning, already-linked account opens straight to its actual state instead of restarting the
  wizard from scratch. Shows a completion banner with a "Continue to Chats" link once both are done
  - The credentials step opens with a numbered walkthrough for getting an API ID/hash from
    my.telegram.org (links open in a new tab; Telegram's own labels such as "API development tools"
    are kept in English in every language, since that's what the person has to match on its page)
  - The archive card offers "Create archive database" only when the API reports `unattached`. Not
    for `unavailable` (a reference already exists, so `POST /archive/provision` would always 409)
    and not when the status couldn't be fetched at all. Its buttons sit in one row with any error
    shown beneath them, so an error no longer rearranges or crowds the buttons
  - `POST /auth/register` now redirects here instead of `/index.html` (`web/js/views/register.js`),
    since a brand-new account can't do anything useful until this is done. A CLI-created account
    (`manage_admin.py create`/`promote`) is unaffected - it never goes through `/auth/register` -
    and reaches the same page later via the Settings link below if it needs to
- Settings tab (`web/js/views/settings.js`) - kept deliberately lean: today it's a single card
  linking to the setup page above. This tab is where future account-settings features (change
  password, etc.) will land one at a time, without ever having to hold the Telegram wizard's state
  machine alongside them
- `GET /telegram/status` (`api/routes/telegram.py`) - read-only `{has_credentials, has_session}`
  summary backing the setup page above, added specifically so a UI could ask "how far did this
  account get?" without exposing the encrypted values themselves (see `TelegramStatusOut`'s own
  docstring)
- Wired the 11 Telegram-flow and 2 archive-provisioning error `reason` codes into
  `web/js/lib/errors.js` / `i18n/{en,uk}.js` - previously deferred until there was a UI to show
  them on; the setup page above is that UI
- **Unlink Telegram** - `DELETE /telegram/session` (`api/routes/telegram.py`,
  `control_db.queries.clear_telegram_session`) clears only the saved session, deliberately keeping
  `telegram_api_id`/`telegram_api_hash` and `archive_db_ref` untouched, so relinking skips the
  credentials step and no archived history is affected. Idempotent - calling it with nothing to
  unlink still succeeds. `web/js/views/telegram-setup.js`'s "Linked" step gained an "Unlink
  Telegram" button (confirmed via `window.confirm()`, same pattern as the archiver Stop button) next
  to the existing "Relink a different account" one
- **Admin badge next to the "TeleVault" wordmark** - a small "Admin" pill (`#admin-badge`, reusing
  the existing `.info-badge` style) on both `index.html`'s nav rail and `telegram-setup.html`'s
  header, shown for accounts whose access token carries `is_admin: true` (decoded client-side purely
  for this display - never a substitute for the server-side check every admin-only endpoint already
  does). Both wordmarks are now also links back to `/index.html`

### Fixed

- Health tab (`web/js/views/health.js`) showed a red "✗ Database readable" for an account whose
  archive simply isn't set up yet (`archive_status: "unattached"`), directly above a message saying
  exactly that. That state now gets a neutral "·" row ("Archive database not set up yet"), the same
  treatment the session row already used for "not applicable". `unavailable` (a ref exists but
  couldn't be reached) is a genuine failure and keeps the ✗
- **A misconfigured `archive_db_ref` (one that names a database that was never actually `CREATE
  DATABASE`'d) was indistinguishable from an ordinary, transient "Postgres is briefly unreachable"
  outage** - both surfaced as `archive_status: "unavailable"` and told the person to "try again
  shortly", which can never work for the former. `db.is_missing_database_error()`
  (`db/connection.py`) tells the two apart via the underlying Postgres SQLSTATE (`3D000`,
  invalid_catalog_name); `GET /api/health`, `get_archive_connection()`
  (`api/dependencies.py`), and the Health tab / setup page's archive card now surface a distinct
  `"misconfigured"` status with "contact your administrator immediately" wording instead
- `telegram-setup.html`'s header read as glued to its own intro paragraph - `.auth-card__subtitle`'s
  `-8px` top margin (tuned for the compact login/register card it was designed for) pulled the
  subtitle up almost onto the wordmark's own line-height. Scoped override
  (`.setup-page__header .auth-card__subtitle`) removes just that negative margin on this page
- `db/connection.py`'s new `is_missing_database_error()` referenced `OperationalError` in its own
  signature without importing it - would have raised `NameError` at import time and taken the whole
  app down on startup. Added the missing `sqlalchemy.exc.OperationalError` import
- The four archive-reading tabs (`chats.js`/`messages.js`/`deleted.js`/`stats.js`) tracked only the
  last *successful* page for their `televault:langchange` reactive re-render, silently doing nothing
  for an error state - so an error message (e.g. "archive not set up yet") stayed frozen in whatever
  language was active when it first appeared, only picking up a language change on the next real
  reload or tab re-entry (which re-fetches). All four now track a single tagged-union
  `lastRender` (`data` / `error` / `networkError`) and share one `redraw()` used by both the initial
  load and the langchange listener, so every state - not just the successful one - re-renders
  immediately on a language switch
- `web/sw.js`'s `CACHE_NAME` was never bumped across several commits that changed precached shell
  files (`errors.js`, `i18n/{en,uk}.js`, `health.js`, `app.js`, `base.css`) - the service worker's
  fetch handler is strict cache-first with no revalidation, and the browser only re-runs `install`
  (which is what actually re-fetches `SHELL_FILES`) when `sw.js` itself changes byte-for-byte, so
  those edits could sit silently stale through any number of ordinary reloads. Bumped to v19 (fixes
  the staleness) and added `login.html`/`register.html`/`telegram-setup.html` and their own JS,
  `lib/auth.js`, `lib/chat-filter.js`, and the new `lib/archive-error.js` below, all previously
  missing from `SHELL_FILES` entirely (never stale, just never precached)

### Added (continued)

- **License: PolyForm Noncommercial 1.0.0** (`LICENSE`, verbatim from the PolyForm project) plus `NOTICE` with the
  required copyright line; README gets a License section. Use, modification and sharing are allowed for
  noncommercial purposes; commercial use needs a separate agreement
- **Instance-specific legal pages:** `/tos` and `/privacy` now serve `instance/legal/<name>.html` when it exists,
  falling back to the tracked placeholder templates in `web/`. `instance/` is git-ignored, so an operator's real
  name/contact/jurisdiction never reach the public repo. A startup warning is logged while a template is being served

- **Terms of Service and Privacy Policy** pages at `/tos` and `/privacy` (`web/tos.html`, `web/privacy.html`;
  clean URLs served by explicit routes in `api/server.py`, since `StaticFiles` only maps real file names).
  Written against what the code actually stores and does (Argon2 password hashes, encrypted Telegram
  credentials/session, IP + user-agent in the security log, edit history, Google Fonts). Contain highlighted
  `[PLACEHOLDER]` fields the instance operator must fill in (name, contact email, jurisdiction, hosting)
- **Site footer** on every page: `TeleVault © 2026 | Private Beta | Terms of Service | Privacy Policy`
- **Mandatory consent checkbox on registration** ("I agree to the Terms of Service and acknowledge the Privacy
  Policy", both names link to the documents in a new tab). Enforced server-side (`accepted_terms`, 400
  otherwise) and recorded per account in new `users.terms_accepted_at` / `users.terms_version` columns
  (migration `b7d2e91f4a63`; `CURRENT_TERMS_VERSION` in `api/routes/auth.py` is the version stamp - bump it
  when the documents change materially). Existing accounts and shell-created admins have NULL = no recorded
  acceptance

- **Non-affiliation disclaimer** (EN/UK) on the sign-in and register cards, at the bottom of Settings, and
  at the top of the README

- **Custom 404 page** (`web/404.html`): unknown non-API URLs now get a themed, bilingual "This page wasn't
  archived" page with a link back to `/`, served with a real 404 status (Starlette's `StaticFiles` does this
  automatically once the file exists). `api/server.py`'s `_WebStaticFiles` keeps unknown `/api/...` paths on
  the plain JSON 404 that `apiFetch` and scripts expect. The page makes no API calls, so it works signed in,
  signed out, or with the API down
- **Copy button for new invite tokens** in the admin panel (Clipboard API, with a select-the-text fallback
  on plain HTTP / refused permission). Worth having because the token is shown exactly once

- **Admin panel: search, pagination, invite status and cleanup** - both lists get a client-side search box
  (users by name; invites by redeemer's name or `#id` - never by token, which the API deliberately never
  returns again) and the shared pager. Invites now show Active / Expired / Used, and unused ones (expired
  or still valid) can be deleted via `DELETE /admin/invites/{id}` (404 unknown, 409 already used -
  used invites stay as registration history). Deleting doubles as revoking a leaked token. There is
  intentionally no "revive expired invite"
- **In-app dialogs** (`web/js/lib/dialog.js`: `showAlert` / `showConfirm` / `showTypedConfirm`) replace every
  native `alert()` / `confirm()` / `prompt()`, so a browser's "stop showing dialogs" checkbox can no longer
  silently disable a destructive-action guard. The typed-username delete confirmation now explains a
  blank/mismatched entry inline instead of doing nothing

- **Single-admin model.** TeleVault now has exactly one admin account, enforced at the database level
  by a partial unique index (`ix_users_single_admin` on `users.is_admin WHERE is_admin = true` -
  `control_db/schema.py`, migration `e7e73e114fac`). The old `promote_user_to_admin()` query function
  and `scripts/manage_admin.py`'s `promote` subcommand are removed entirely - there is no "grant admin"
  path any more, by design
- **Bootstrap-admin-token registration** - solves the chicken-and-egg problem of the very first admin
  needing an invite that only an admin could have created. An operator sets
  `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` in `.env` (`config.py`); the first person to register through the
  ordinary `POST /auth/register` with that exact value (constant-time compared) becomes the admin
  automatically, indistinguishably from an ordinary registration from the caller's point of view. Works
  exactly once per instance - `control_db.queries.admin_exists()` gates it, and the underlying unique
  index makes a second admin impossible even under a race. `scripts/manage_admin.py`'s `create`
  subcommand remains as a shell-access fallback and now refuses if an admin already exists
- **Admin panel API** (`api/routes/admin.py`, all routes gated on the new `require_admin` dependency in
  `api/dependencies.py`): `GET /admin/users` (list every account), `POST /admin/users/{id}/lock` and
  `/unlock`, `DELETE /admin/users/{id}` (full account deletion, see below), `POST /admin/invites` and
  `GET /admin/invites` - bringing most of `scripts/manage_admin.py`'s capabilities into the API/UI.
  `create` (bootstrapping), `delete-admin`, and `set-archive` deliberately remain CLI-only
- **Full, irreversible account deletion** - `control_db.queries.delete_user_completely()` removes the
  `users` row itself (so the username becomes reusable), every refresh token (`ON DELETE CASCADE`), and
  nulls out (not deletes) any invite `used_by`/audit-log `user_id`/`actor_id` reference that pointed at
  the account (`ON DELETE SET NULL` - migration `e7e73e114fac`), keeping invite/audit history intact
  without it pointing at a row that no longer exists. `db/deprovisioning.py`'s new
  `drop_archive_database()` then drops the account's archive database entirely, mirroring
  `db/provisioning.py`'s create path. Reachable via `DELETE /admin/users/{id}` for ordinary accounts, or
  `scripts/manage_admin.py delete-admin` (never wired to HTTP) for the admin account itself
- **Safety check before Telegram unlink or account deletion**: `api/process_utils.py`'s new
  `running_process_reason()` refuses (409, reusing the existing `already_running`/`backfill_running`
  reasons) to unlink a Telegram session (`DELETE /telegram/session`) or delete an account
  (`DELETE /admin/users/{id}`, `scripts/manage_admin.py delete-admin`) while the live userbot or a
  backfill is running against this instance - pulling a session or dropping a database out from under
  either would corrupt or crash it
- **Self-fix links for recoverable errors.** New `web/js/lib/archive-error.js`, shared by
  `chats.js`/`messages.js`/`deleted.js`/`stats.js`, and `health.js`'s own archive message: an
  `archive_unattached` error now includes a "Go to settings" link to `telegram-setup.html`. Deliberately
  never shown for `archive_unavailable` (transient - nothing to click) or `archive_misconfigured`
  (admin-only - the database was never created; a settings link couldn't fix it)

- **Admin panel UI** (`web/js/views/admin.js`, new "Admin" nav tab in `index.html`, hidden for non-admins):
  wires `api/routes/admin.py` into the app - a Users card (lock/unlock, and delete with a
  type-the-username-to-confirm prompt, same posture as `scripts/manage_admin.py`'s own `delete-admin`) and
  an Invites card (create a token with a configurable expiry, shown once; a history list of past invites)
- **Current username shown under the "TeleVault" wordmark** on both `index.html` and `telegram-setup.html` -
  plain, muted text, deliberately not a link or button, so it's easy to tell which test account you're
  logged in as without it competing for attention. Fetched once via `GET /auth/me` and cached
  (`lib/auth.js`'s new `fetchCurrentUser()`) rather than added as a JWT claim, to avoid touching token
  issuance for a purely cosmetic reminder
- `.env.example` was missing `TELEVAULT_BOOTSTRAP_ADMIN_TOKEN` entirely - added, with the same
  `openssl rand` convention as `FERNET_KEY`/`JWT_SECRET`

### Changed (continued)

- **New colour palette** (light: lavender, dark: midnight + orange) in `variables.css`; every text pair passes
  WCAG AA. The `seal-bg` / `patina-bg` tints are now faint tints of their accents (they had been set equal to the
  panel colour, which would have erased the deleted/edited row highlighting), and links in the legal pages /
  registration checkbox are underlined, because the light-theme accent is almost as dark as body text
- **Self-hosted fonts** replace Google Fonts: Tektur (display), Sofia Sans (UI), IBM Plex Mono (mono), all SIL OFL
  1.1, subsetted to Latin + Cyrillic woff2 in `web/fonts/` with their license texts, declared in `css/fonts.css`
  and precached by the service worker. No page makes a third-party font request any more; the Privacy Policy no
  longer mentions Google Fonts or a hosting provider by name

- **Deleting an account now purges its security-log rows** (`auth_audit_log` rows with that `user_id`,
  which carry its IP address and user-agent) instead of just de-linking them; only a minimal, IP-less
  `user_deleted` record remains. Privacy Policy updated to match
- Footer credits the author: `TeleVault © 2026 gvneshv` (links to the GitHub profile)

- **API docs are now opt-in:** `/api/docs`, `/api/redoc` and `/api/openapi.json` are only served when
  `ENABLE_API_DOCS=true` is set in `.env` (default off - they publish a full map of the API). Add that line
  to your local `.env` to keep using Swagger UI while developing

### Fixed (continued)

- **Sign-in / registration errors are now localized.** `api/routes/auth.py` returned plain English strings, so
  "Incorrect username or password." etc. appeared in English in the Ukrainian UI. They now use the project's
  `{message, reason}` shape (`_auth_error()`), and `lib/errors.js` + `i18n/{en,uk}.js` translate the reason codes
- **Footer sits at the bottom of every tab** (column-flex `.app-main`; `telegram-setup` page likewise). The Admin
  tab's section was being appended after the footer, which put the footer above its content; it is now inserted before it
- Removed the in-app non-affiliation disclaimer (sign-in/register cards, Settings): the Terms of Service now carry it

- `db/queries.py`: `get_message()` and `get_deletion_record()` now return a plain `dict` (as annotated) instead of
  SQLAlchemy's `RowMapping`, which fixes the type-checker error

- Sign-in/registration no longer show "[object Object]" when the server rejects input with a structured
  validation error (HTTP 422); the page now falls back to a localized "please check your input" message

- A failed sign-in (401) now clears the password field and refocuses it; the username is kept. Not done for
  403 (locked) or 429 (rate limited), where the password was correct. Registration keeps all fields on
  errors about other fields (username taken, bad invite) and clears only the passwords when the server
  rejects the password itself (422). `lib/auth.js` now attaches the HTTP status to errors thrown by
  `login()` / `register()` so pages can tell these cases apart
- Small gap between the invite hours/Create row and the token result / search box in the admin panel

- The Admin tab was visible to non-admins: `.app-nav__link { display: flex }` overrides the `hidden`
  attribute. Added a global `[hidden] { display: none !important }` and, beyond that, the tab (button,
  section, and `views/admin.js` itself) is now created/loaded only for the admin account instead of shipped
  hidden in `index.html`
- Visual polish: `.info-badge` pills (Admin / Locked) vertically centered; invite hours field and Create
  button share one height; user metadata is " · "-separated

- Three pre-existing static-typing gaps in `api/routes/telegram.py` (not runtime bugs, but real strict-mode
  type-checker complaints): `encrypt_secret()`/`decrypt_secret()`'s generic `str | None -> str | None`
  signature was triggering `str | None` vs `str` mismatches at every call site in this file (the only
  caller of either function anywhere in the codebase) even though each call site already guarantees a
  non-`None` input - added explicit `assert ...is not None` at each site, which satisfies the type checker
  and doubles as a real guard (these are nullable columns; a silent `None` getting through would otherwise
  store `NULL` instead of failing loudly). Telethon's own type stubs separately mistype
  `TelegramClient.disconnect()` and `.session` as non-awaitable/`None` - narrow, commented
  `# type: ignore[...]` added at each of those four call sites instead, since that one's a stub gap, not
  ours to fix
- Migration `e7e73e114fac`'s `ix_users_single_admin` can fail with a `UniqueViolation` on an instance that
  has more than one `is_admin = true` row already (e.g. from testing `scripts/manage_admin.py create`
  before the single-admin model existed) - resolve by demoting every row but the one real admin
  (`UPDATE users SET is_admin = false WHERE id <> <id>;`) before re-running `alembic upgrade head`; this
  is a one-time manual data-cleanup step, not something the migration itself should paper over

### Planned - Phase 3 (Advanced Features)

- Ingestion-time chat allowlist (the blocklist half shipped, see "Unreleased"): archive *only*
  chosen chats. Needs a way to see chats that are not archived yet, which means storing
  metadata about chats the person chose not to keep - undecided
- Chat view (design approved, see `claude/chat-view-spec.md`; backend step A shipped, see "Unreleased"): a Telegram-like view of one chat showing every archived message, deleted ones
  (marked) and edited ones included; clicking an edited message expands its edit history with the date of each version. A chat-list panel next to the conversation REPLACES the
  Chats, Messages and Deleted pages; the main navigation becomes Chats, Stats, Health, Backfill, Settings (+ Admin) and can collapse to icons. The header holds "Block this chat" and
  "Delete this chat's data". Remaining: shell, panel + conversation, polish
- Storage mode: `all` (default) vs `deletions_only`
- Display name overrides: map a friendly label to a sender ID
- Username change tracking (`username_history` table)
- Auto-read policies: mark selected chats as read on a schedule
- Data management: clear all data (per-chat data removal: backend shipped, button comes with the chat view)
- Message ignore rules: filter by text pattern before archiving (filtering by chat shipped
  as the ingest blocklist)
- TTL / retention policy: auto-delete archived messages older than N months
- Expose `SenderOut.resolved_name` as a computed API field instead of the
  same priority logic being duplicated in three places (the backend
  property, `messages.js`, `deleted.js`)

### Planned - Phase 4 (Infrastructure & Expansion)

- Media archiving (photos, documents, voice notes) with disk management
- Orphan cleanup scheduler: reconcile DB records vs files on disk
- Notifications: Web Push via service worker (requires HTTPS)
- Read receipt inference: track `read_inbox_max_id` per chat (private chats only)
- Reactions tracking (`message_reactions` snapshot table) - low priority

---

## [2.1.0] - 2026-09-13

### Summary

Reliability fixes for the Postgres migration shipped in [2.0.0] - several
of which only surfaced under real usage after that release (a stopped
Docker container, a deleted `data/` directory, Postgres disappearing
mid-run) - plus two new features: a chat filter for the Messages and
Deleted views, and pagination for the Backfill history table.

### Added

- Chat filter dropdown (Messages, Deleted views) - checkbox multi-select
  across every archived chat, with a pinned "All chats" default, in-panel
  search, and a per-view persisted selection (`localStorage` - Messages
  and Deleted deliberately keep independent selections). Backed by a new
  `GET /api/chats/options` endpoint (unpaginated `{chat_id, name}` list)
  and a repeatable `chat_ids` query param on `/api/messages` and
  `/api/deleted`
- Pagination for the Backfill history table - `GET /api/backfill/history`
  now takes `page`/`per_page` (default 20) and returns the same
  `PaginatedResponse` envelope every other list endpoint uses, replacing
  an unpaginated `LIMIT 50`
- `db/connection.py`'s `check_connection()` - lets `main.py`/`backfill.py`
  verify Postgres is actually reachable immediately at startup, instead of
  only discovering it's not on the first real query

### Fixed

- **Heartbeat file crash on shutdown, and a resulting false "not running"
  status** - nothing ensured `data/` existed before writing into it; this
  was previously an implicit side effect of SQLite living there, and broke
  once storage moved to Postgres and that directory was later deleted. The
  very first heartbeat write then failed with `FileNotFoundError`, an
  exception that only surfaced on shutdown (when it was finally awaited),
  while the heartbeat file itself was never actually written - so the web
  UI never showed the archiver as running, and the single-instance guard
  (which reads that same file) let a second `main.py` start and corrupt
  Telethon's own session file. Fixed by ensuring `data/` exists before
  every heartbeat/status write (`main.py`, `utils/atomic_write.py`)
- **`main.py`/`backfill.py` reporting healthy with no reachable
  Postgres** - `init_db()` only builds the SQLAlchemy `Engine`, it never
  opens a connection, so a stopped Docker container went unnoticed until
  the first real query hung. Both now call `check_connection()` right
  after `init_db()` and exit with a clear message if it fails
- **Multi-minute hang, then a forced double Ctrl-C, when Postgres
  disappeared mid-run** - the `connect_timeout` added alongside
  `check_connection()` only bounds establishing a *new* connection; an
  already-open one that goes dead without a clean FIN/RST fell back to the
  OS's own TCP retransmission timeout (on the order of minutes). Added TCP
  keepalives (`keepalives_idle`/`interval`/`count`) so a dead connection is
  noticed in roughly 14 seconds instead
- **DB-connectivity failures logged as a full connection-pool traceback**
  in `handlers/on_message.py`, `on_edit.py`, `on_delete.py` - now caught
  specifically as `sqlalchemy.exc.OperationalError` and logged as one
  readable line; genuinely unexpected errors still get the full traceback.
  `on_delete.py`'s `_persist_deletions()` additionally had no exception
  handling at all around its DB connection, so a failure there escaped to
  Telethon's own generic handler instead of TeleVault's own logging - now
  caught the same way

### Changed

- Active nav-tab styling (Chats/Messages/Deleted/Stats/Health) - replaced
  the plain inset left-border accent with a glowing ring
  (`--color-focus-glow`, a new per-theme CSS variable), in both light and
  dark themes
- **Breaking (internal API):** `/api/messages` and `/api/deleted` no
  longer accept the singular `chat_id` query param - replaced by a
  repeatable `chat_ids`, matching the new chat-filter dropdown. Not
  externally versioned (single-user, self-hosted), but noted here since
  it's a genuine parameter removal, not an addition

---

### Summary

Full migration of the storage layer from SQLite to PostgreSQL (SQLAlchemy
Core + Alembic + psycopg v3). A major-version bump rather than a minor one:
this is not a drop-in upgrade - existing installs need Postgres running
(Docker Compose provided), `alembic upgrade head` run once, and, for anyone
with an existing SQLite archive, a one-time data migration script. The
application's own behavior (archiving, search, the web UI) is functionally
unchanged; what changed is entirely underneath it. See README's updated
setup steps 2–4.

### Added

- `alembic/` - Alembic initialized, pointed at `db/schema.py`'s metadata and
  `config.py`'s `DATABASE_URL` (not duplicated into `alembic.ini`)
- `alembic/versions/..._baseline_schema.py` - baseline migration: all 7
  tables and 9 indexes (autogenerated from `db/schema.py`), plus
  hand-written `CREATE EXTENSION pg_trgm` and 6 PL/pgSQL trigger functions
  maintaining `chats`' denormalized counters (`message_count`,
  `deleted_count`, `edited_count`, `first_message_at`, `last_message_at`,
  `last_message_preview`) - ported from the old SQLite migration 005's
  triggers. Two genuine (not just syntactic) translation issues: SQLite's
  2-argument `MIN`/`MAX` scalar functions have no Postgres equivalent
  (Postgres's are aggregate-only) - replaced with `LEAST`/`GREATEST`; and
  `is_edited`/`is_deleted` are native `BOOLEAN` now, so counter arithmetic
  needs an explicit `::int` cast. Verified end-to-end against a live
  Postgres instance (upgrade, trigger behavior under real writes,
  downgrade, re-upgrade).
- `scripts/migrate_sqlite_to_postgres.py` - one-time data migration for
  existing SQLite archives. Per-chat atomic transactions make it safely
  resumable after any interruption - verified with a real simulated
  mid-chat crash (confirmed zero partial rows persist, and a re-run cleanly
  completes only the interrupted chat). Handles two distinct datetime
  string formats present in the same source database, verified against the
  actual pre-migration code rather than assumed. `--dry-run` and `--verify`
  modes; built-in post-migration row-count verification across all 7 tables.
- `docker-compose.yml`, `docker/init/01-extensions.sql` - local dev Postgres
  16 instance with `pg_trgm` enabled on first start
- `db/connection.py`'s `get_readonly_connection()` - read-only session
  enforcement via `SET default_transaction_read_only`, replacing SQLite's
  `file:...?mode=ro` URI trick

### Changed

- `db/schema.py` - rewritten from raw SQLite DDL strings to SQLAlchemy Core
  `Table` objects. `BigInteger` for every Telegram ID column (SQLite's
  `INTEGER` was already 64-bit, silently hiding the fact that Postgres's
  plain `integer` is only 32-bit), `TIMESTAMPTZ` replacing TEXT-stored ISO
  datetimes, native `BOOLEAN` replacing `INTEGER` 0/1, and a single
  `pg_trgm` GIN index directly on `messages.text` replacing the FTS5
  virtual table + 3 sync triggers
- `db/connection.py` - single global `sqlite3.Connection` replaced with a
  SQLAlchemy `Engine` + connection pool (psycopg v3). `get_connection()` is
  now "checkout, use, close" rather than "the one connection held open for
  the app's lifetime" - a real contract change, not just an implementation
  swap; verified against live Postgres, including a rollback-ordering bug
  caught in testing (a rejected write on a read-only connection left it in
  an aborted-transaction state that had to be explicitly cleared before
  returning it to the pool)
- `db/queries.py`, `db/read_queries.py` - ported to SQLAlchemy Core.
  `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING`, `cursor.lastrowid` →
  `RETURNING id`, `UPDATE OR IGNORE` (no Postgres equivalent) → a
  `NOT EXISTS`-guarded bulk `UPDATE` in `merge_chat()`. FTS5 `MATCH` search
  replaced with `ILIKE` + `pg_trgm` - verified wildcard-escaping actually
  works (a bare `%` or `_` search matches only literal characters, not
  every row) rather than assumed
- `handlers/on_message.py`, `on_delete.py`, `on_edit.py` - DB writes now run
  via `asyncio.to_thread()` rather than inline: a Postgres round-trip would
  otherwise block Telethon's entire event loop for its duration, unlike
  SQLite's negligible local file I/O. Verified DB work genuinely runs off
  the event-loop thread, not just wrapped
- `api/dependencies.py` - `get_db()` now yields from
  `get_readonly_connection()` instead of opening a raw `sqlite3` connection
  in `ro` URI mode
- `main.py`, `backfill.py` - `init_db()` now takes a `DATABASE_URL`, not a
  file path; schema application is `alembic upgrade head` (a deploy step),
  not a function called at every startup
- `api/routes/backfill.py` - cancel endpoint's write path fixed: it was
  opening a raw `sqlite3` connection directly in a route file, which would
  have broken outright with SQLite gone, and was also a genuine write that
  can't go through `get_db()`'s now-read-only connection

### Fixed

- `handlers/on_delete.py`'s no-`chat_id` fallback path
  (`_flag_deleted_without_chat`) had its own untouched raw SQL using SQLite
  `?` placeholders and `is_deleted = 0` - would have errored immediately
  against Postgres's native `BOOLEAN` column type the first time a
  private-chat deletion arrived with no `chat_id`
- `handlers/on_edit.py` - sender-upsert guard condition was inverted
  (`is None` instead of `is not None`), passing `None` into a non-optional
  parameter. Pre-existing bug, unrelated to this migration; found and fixed
  while already touching this file
- 5 route files (`deleted.py`, `messages.py`, `stats.py`, `chats.py`,
  `health.py`) had stale `sqlite3.Connection` type hints left over from
  before `api/dependencies.py`'s return type changed - cosmetic only (these
  routes never touch `sqlite3` directly; `db/read_queries.py` handles their
  actual queries), corrected for accuracy

### Schema (Postgres baseline)

```
chats             (chat_id PK, name, username, chat_type, first_seen,
                   message_count, deleted_count, edited_count,
                   first_message_at, last_message_at, last_message_preview)
senders           (sender_id PK, username, first_name, last_name, first_seen)
messages          (id PK IDENTITY, tg_message_id, chat_id FK, sender_id FK,
                   text, date, is_edited BOOLEAN, edited_at,
                   is_deleted BOOLEAN, deleted_at, archived_at)
                  UNIQUE INDEX on (tg_message_id, chat_id)
                  GIN index (pg_trgm) on text
message_edits     (id PK IDENTITY, message_id FK, old_text, new_text, edited_at)
message_deletions (id PK IDENTITY, message_id FK, text_snapshot, deleted_at,
                   deleted_by_inference, inference_confidence)
chat_migrations   (old_chat_id PK, new_chat_id, migrated_at)
backfill_runs     (id PK IDENTITY, started_at, finished_at, status,
                   chat_selector, chats_total, chats_done,
                   messages_stored, messages_skipped, error_message)
```

### Technical decisions

- SQLAlchemy Core (not the ORM) + psycopg v3 + Alembic
- `asyncio.to_thread()` wraps only the 3 live-handler DB call sites - not a
  move to full async, since API routes are sync (`def`) and don't need
  converting
- `pg_trgm` chosen for full-text search over native Postgres `tsvector`,
  which can't do mid-word/substring/Cyrillic matching
- Denormalized `chats` counters maintained via Postgres triggers (ported
  from the equivalent SQLite ones), not recomputed in application code
- Existing SQLite archives are migrated via a dedicated one-time script
  with its own atomicity/resumability guarantees, not folded into
  `db/queries.py`'s normal write path
- `is_edited`/`is_deleted` moved from `INTEGER` 0/1 to native `BOOLEAN` -
  decided over keeping 0/1 for parity, since psycopg maps `BOOLEAN` straight
  to Python `True`/`False`

---

## [1.2.0] - 2026-09-07

Last release on the SQLite-based storage layer - the PostgreSQL migration
begins from here (see "Planned - Phase 4" above).

### Fixed

- Backfill progress bar sometimes failing to appear when starting a run,
  requiring a manual tab switch to recover. Caused by an `awaitingStart`
  guard (added to protect against a stale-terminal-state race) being reset
  on the very first post-start poll, which almost always still reads
  `"idle"` before the new subprocess has written anything.
- `main.py` and `backfill.py`, when launched directly from a terminal,
  bypassed the mutual-exclusion checks that only fired when started through
  the API/web UI - allowing two live Telethon sessions, or a session and an
  in-progress backfill, to run concurrently. Most likely cause of a spurious
  2FA re-prompt seen in practice. Both entry points now guard themselves
  directly, via a new shared `is_archiver_running()` / `is_backfill_running()`
  pair in `api/process_utils.py`.
- `utils/__inti__.py` typo corrected to `utils/__init__.py`. The package init
  was never actually being loaded (the app only worked because Python 3.3+
  namespace packages don't require one) - its intended
  `from utils import setup_logging` re-export was dead code.

### Added

- `scripts/toggle_archiver.ps1` / `.bat` - a double-click Windows shortcut
  that starts or stops the live archiver directly (reads/writes the same
  heartbeat file `main.py` itself uses; no API server required).

---

## [1.1.0] - 2026-07-09

### Summary

Phase 2 complete: a read-only REST API and a full vanilla-JS/HTML/CSS PWA
web UI, covering all five planned views (Chats, Messages, Deleted, Stats,
Health). Backward-compatible with Phase 1 - the userbot process is
unchanged; the API server is a separate process that only reads the
shared SQLite file.

### Added - Archiving (`handlers/`)

- Call service messages now stored with humanized text labels (e.g.
  `[Missed call]`, `[Voice call · N min]`) instead of `NULL`

### Added - API (`api/` package)

- FastAPI + Uvicorn REST API, mounted under `/api`, with the static web UI
  served from `/` by the same process (`api/server.py`)
- Read-only DB access via `api/dependencies.py`'s `get_db()` - opens SQLite
  in `mode=ro` URI mode so the driver hard-errors on any accidental write,
  since the userbot and API share one file
- Routes: `GET /api/chats`, `/api/chats/{id}`, `/api/chats/{id}/messages`,
  `/api/messages`, `/api/messages/{id}`, `/api/deleted`, `/api/stats`,
  `/api/health`
- `db/read_queries.py` - read-only query layer backing all routes above;
  returns plain dicts, no ORM
- Pydantic v2 response schemas (`api/schemas/`): `ChatOut`, `ChatSummary`,
  `MessageOut`, `MessageDetail`, `SenderOut`, `EditOut`, `DeletionOut`,
  `StatsOut`, `ChatStatRow`, `PaginatedResponse`, `HealthOut`
- `LOWER_UNICODE()` custom SQL function registered per-connection - SQLite's
  built-in `LOWER()` only folds ASCII case, so this was needed for
  case-insensitive search across non-Latin scripts (e.g. Cyrillic)

### Added - Web UI (`web/` package)

- App shell: nav-rail + content-pane layout, "archive paper" (light) /
  "vault steel" (dark) themes via CSS custom properties, EN/UK i18n
  persisted to `localStorage`, PWA manifest + service worker (deliberately
  excludes `/api/*` from the cache - this is private data, and stale cached
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
  count) with manual refresh - no polling
- Shared `web/js/lib/`: `dom.js` (`escapeHtml`, `highlightMatches`),
  `pagination.js` (render + wire-up) - extracted once a second view needed
  identical logic, rather than duplicated per view
- The "wax-seal" `seal-badge` signature element, reused for deleted-message
  counts

### Added - Deletion actor inference (channel-only)

- `deleted_by_inference` now actually computes something: `'channel_admin'`
  for messages deleted from a broadcast channel, since regular subscribers
  cannot delete channel posts - only admins can, making this a structural
  fact rather than a guess
- Deliberately **not** implemented for private/group/supergroup chats -
  Telegram allows any party to delete a message for everyone with no time
  limit and no record of who did it, so a sender_id/timing guess there
  would be closer to a coin flip than a signal. This was the original
  Phase 1 plan (see migration 001); reviewed and dropped during Phase 2 in
  favor of leaving those permanently `'unknown'`
- New migration `db/migrations/002_channel_admin_only_inference.py` -
  rebuilds `message_deletions`' CHECK constraint from
  `('self', 'other', 'unknown')` (never actually used - see Fixed below) to
  `('channel_admin', 'unknown')`

### Fixed

- **`chat`/`sender` always null in `/api/messages`, `/api/deleted`, and
  `/api/messages/{id}`** - the SQL correctly joined `senders`/`chats`, but
  nothing reshaped the flat, prefixed columns (`sender_id`, `chat_name`...)
  into the nested objects `MessageOut` expects, so Pydantic silently
  defaulted both to `null` on every row. Added `_shape_message_row()` in
  `db/read_queries.py`; `get_message_detail()` also gained a `chats` join
  it was missing entirely. Verified against a live SQLite DB, not just
  parse-checked
- **Docstrings describing deletion-actor inference as implemented when it
  wasn't** - migration 001's and `DeletionOut`'s docstrings both described
  a "best-effort guess based on sender_id and timing" that, on inspection,
  no code anywhere actually computed; every row got `'unknown'` purely from
  the column default. Corrected to describe the actual (and, since this
  release, final) channel-only design
- **Page-breaking `const` redeclaration** between `messages.js` and
  `deleted.js` (both declared an unprefixed `SEARCH_DEBOUNCE_MS`) - none of
  `web/js/*.js` use ES modules, so top-level `const`/`let` share one global
  lexical scope across every `<script>`-loaded file; the second file to
  load threw a `SyntaxError` at parse time, breaking the entire page.
  Renamed to per-view-prefixed constants; see Phase 3's planned ES-module
  migration above for the structural fix
- Favicon 404 - file was placed at the project root, which `uvicorn` never
  serves (only `web/` is mounted); moved to `web/favicon.ico`, added an
  explicit `<link rel="icon">` tag
- `seal-badge`'s signature tilt (`rotate(-1.5deg)`) misaligning rows in the
  Chats view - a fixed rotation angle displaces wider boxes higher than
  narrower ones, and badge width varies with the deleted-count text.
  Scoped `transform: none` override for that context only; the tilt is
  unchanged everywhere it's used in isolation
- Chats-view labels (deleted count, chat type, pagination) not re-translating
  on a language switch - they're built dynamically from fetched data, not
  marked `data-i18n`, so `applyTranslations()` had no way to reach them.
  `i18n.js` now dispatches a `televault:langchange` event after switching;
  views listen and re-render their cached data

### Changed

- `.chat-type-badge` renamed to `.info-badge` - needed as a generic neutral
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
  fires for **both channels and supergroups**, not channels exclusively -
  important because they have different deletion permissions (channels:
  admins only; supergroups: members can typically delete their own
  messages). The channel-only actor inference above checks `chat_type`
  explicitly rather than assuming from which event type fired

---

## [1.0.0] - 2026-05-30

### Summary

Phase 1 complete. Real-time text message archiving across all chat types.
Deleted messages are flagged and preserved. Edit history is tracked.
Stable enough for always-on deployment.

### Added

- `main.py` - async entry point; graceful shutdown on Ctrl-C (all platforms) and SIGTERM (Unix)
- `config.py` - frozen `Settings` dataclass loaded from `.env` via `python-dotenv`
- `db/connection.py` - SQLite connection with WAL mode, FK enforcement, and ISO 8601 datetime adapter
- `db/schema.py` - DDL for `chats`, `senders`, `messages`, `message_edits`, `message_deletions` tables; applied on every startup
- `db/queries.py` - all read/write operations with explicit `commit` / `rollback` (no context-manager transactions)
- `db/__init__.py` - package interface re-exporting `init_db`, `get_connection`, `close_db`, `apply_schema`
- `handlers/on_message.py` - `NewMessage` handler; upserts chat and sender rows defensively before inserting the message
- `handlers/on_delete.py` - `MessageDeleted` handler; falls back to `tg_message_id`-only lookup for private/group chats where `chat_id` is absent
- `handlers/on_edit.py` - `MessageEdited` handler; skips recording when `old_text == new_text` (Telegram fires edits for non-text changes)
- `handlers/helpers.py` - shared utilities: `get_chat_type()`, `get_sender_fields()`
- `utils/logging_setup.py` - console + rotating file handler (5 MB × 3 backups); Telethon pinned to WARNING
- `.env.example` - documented environment variable template
- `.gitattributes` - enforces LF line endings on Windows checkouts
- `requirements.txt` - pinned dependencies: `telethon`, `python-dotenv`
- `README.md` - setup guide, smoke test instructions, project structure, Telegram ToS note

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

- `MessageDeleted` carries no `chat_id` in private/group chats - fallback searches by `tg_message_id` alone
- Scheduled/auto-posted messages bypass `NewMessage`; arrive only as edit events - handled by defensive upsert
- Anonymous admin posts use the group's own (negative) ID as `sender_id` - stored as-is
- `MessageEdited` fires for link preview attachment, keyboard updates, view count increments - skipped when text is unchanged

### Technical decisions

- All datetimes stored as local-timezone ISO 8601 strings without milliseconds
- `detect_types` removed from SQLite connection - converter does not fire reliably in Python 3.14 module context
- `with conn:` (context manager) replaced with explicit `commit` / `rollback` - Python 3.12+ changed context manager semantics, breaking FK visibility across sequential writes
- Each DDL statement committed individually to avoid FK constraint failures under WAL snapshot isolation
- `upsert_chat` / `upsert_sender` accept `commit=False` to allow grouping all inserts into one transaction
- `loop.add_signal_handler(SIGTERM)` wrapped in `try/except NotImplementedError` for Windows compatibility

---

[Unreleased]: https://github.com/gvneshv/TeleVault/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/gvneshv/TeleVault/compare/v2.1.0...v3.0.0
[2.1.0]: https://github.com/gvneshv/TeleVault/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/gvneshv/TeleVault/compare/v1.2.0...v2.0.0
[1.2.0]: https://github.com/gvneshv/TeleVault/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/gvneshv/TeleVault/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/gvneshv/TeleVault/releases/tag/v1.0.0