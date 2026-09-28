/**
 * English (en) translation strings.
 *
 * Keys are namespaced by area (nav.*, health.*, common.*) so this stays organized as views are added in later steps.
 * Add new keys here AND in uk.js together — TeleVaultI18n's t() falls back to the key itself if a translation is missing,
 * so a mismatch won't crash the UI, but it will silently show English/raw keys in the Ukrainian UI.
 * Keep both files in sync as a habit, not just when convenient.
 */

export const en = {
  "app.wordmark": "TeleVault",

  "nav.chats": "Chats",
  "nav.messages": "Messages",
  "nav.deleted": "Deleted",
  "nav.stats": "Stats",
  "nav.health": "Health",
  "nav.settings": "Settings",

  "common.comingSoon": "This view isn't built yet.",
  "common.loading": "Loading…",
  "common.error": "Something went wrong.",
  "common.pageOf": "Page {page} of {pages}",
  "common.pageOfPrefix": "Page",
  "common.pageOfSuffix": "of",
  "common.jumpToPage": "Jump to page",
  "common.first": "First",
  "common.last": "Last",
  "common.prev": "Previous",
  "common.next": "Next",
  "common.newestFirst": "Newest first",
  "common.oldestFirst": "Oldest first",
  "common.type.private": "Private",
  "common.type.group": "Group",
  "common.type.supergroup": "Supergroup",
  "common.type.channel": "Channel",

  "chats.empty": "No chats archived yet.",
  "chats.noPreview": "No messages yet",
  "chats.messagesLabel": "messages",
  "chats.deletedLabel": "deleted",
  "chats.mostRecentFirst": "Most recently active",
  "chats.leastRecentFirst": "Least recently active",

  "messages.empty": "No messages found.",
  "messages.noText": "(no text)",
  "messages.editedLabel": "edited",
  "messages.searchPlaceholder": "Search message text…",
  "messages.onlyEditedLabel": "Edited only",

  "deleted.empty": "No deleted messages found.",
  "deleted.searchPlaceholder": "Search deleted message text…",
  "deleted.viewDetails": "View details",
  "deleted.hideDetails": "Hide details",
  "deleted.noRecord": "No deletion record found.",
  "deleted.actor.channel_admin": "Deleted by a channel admin",
  "deleted.actor.self": "Deleted by you",
  "deleted.actor.unknown": "Deleted by — unknown",
  "deleted.confidence.channel_admin":
    "Only a channel admin can delete a channel post — regular subscribers cannot delete posts.",
  "deleted.confidence.self":
    "Saved Messages is only accessible to you — no one else can see it, let alone delete from it.",

  "stats.totalMessages": "Total messages",
  "stats.totalDeleted": "Deleted",
  "stats.totalEdited": "Edited",
  "stats.totalChats": "Chats",
  "stats.totalSenders": "Senders",
  "stats.archivingSince": "Archiving since",
  "stats.perChatTitle": "Per-chat breakdown",
  "stats.empty": "No chat data yet.",
  "stats.tableChat": "Chat",
  "stats.tableMessages": "Messages",
  "stats.tableDeleted": "Deleted",
  "stats.tableEdited": "Edited",
  "stats.tableLastSeen": "Last message",

  "health.statusOk": "OK",
  "health.statusDegraded": "Degraded",
  "health.dbReadable": "Database readable",
  "health.dbNotSetUp": "Archive database not set up yet",
  "health.sessionExists": "Telegram session found",
  "health.sessionNotApplicable":
    "Only the account that set up this instance's Telegram connection has a session.",
  "health.messageCount": "Archived messages",
  "health.unattached":
    "Your archive hasn't been set up yet. Finish linking your Telegram account to start archiving.",
  "health.unavailable":
    "Your archive database is currently unavailable. Try again shortly, or contact your administrator.",
  "health.refresh": "Refresh",

  "theme.toggleLabel": "Toggle theme",
  "lang.selectLabel": "Language",

  // Backfill additions
  "nav.backfill": "Backfill",

  "backfill.aboutTitle": "About backfill",
  "backfill.aboutIntro":
    "The live archiver only records messages sent while it's running. Backfill fills in the gap: it walks each chat's existing history on Telegram and archives everything TeleVault hasn't seen yet — useful right after your first setup, or for any chat TeleVault only recently joined.",
  "backfill.disclaimerSession":
    "Backfill needs its own Telegram connection. The live archiver (main.py) must be stopped first — Telegram only allows one active session at a time.",
  "backfill.disclaimerDeleted":
    "Messages already deleted before a chat was first archived can never be recovered — Telegram's history API only returns what currently exists.",
  "backfill.disclaimerEdits":
    "Backfilled messages are stored as their current text only. Earlier edited versions from before archiving started cannot be recovered.",
  "backfill.disclaimerApprox":
    "Progress and time remaining are estimates based on Telegram's message counts — treat them as a rough guide, not an exact figure.",
  "backfill.disclaimerBackground":
    "Once started, backfill keeps running on the server even if you close this tab or browser.",
  "backfill.checkingConnection": "Checking live connection…",
  "backfill.connectionOn": "Live archiver is currently connected",
  "backfill.connectionOff": "Live archiver is not connected",
  "backfill.blockedNote": "— backfill is unavailable while it’s running.",
  "backfill.startButton": "Start backfill",
  "backfill.confirmTitle": "Start a backfill?",
  "backfill.confirmBody":
    "This will archive historical messages for the selected chat(s). It can take a long time for large histories.",
  "backfill.warningConnectionOn":
    "The live archiver looks like it's still connected. Stop it before starting a backfill.",
  "backfill.chatLabel": "Chat (optional — leave empty for all chats)",
  "backfill.chatPlaceholder": "@username or numeric ID",
  "backfill.limitLabel": "Message limit per chat (optional)",
  "backfill.limitPlaceholder": "e.g. 500",
  "backfill.confirmStart": "Start",
  "backfill.stateRunning": "Running",
  "backfill.stateCompleted": "Completed",
  "backfill.stateCancelled": "Cancelled",
  "backfill.stateError": "Error",
  "backfill.chats": "chats",
  "backfill.eta": "Est. remaining",
  "backfill.cancel": "Cancel",
  "backfill.historyTitle": "Run history",
  "backfill.noHistory": "No backfill runs yet.",
  "backfill.historyStarted": "Started",
  "backfill.historyStatus": "Status",
  "backfill.historyChats": "New chats",
  "backfill.historyStored": "Stored",
  "backfill.historySkipped": "Skipped",
  "backfill.historyDuration": "Duration",
  "backfill.unitHour": "h",
  "backfill.unitMinute": "m",
  "backfill.unitSecond": "s",

  "messages.wholeWordLabel": "Whole word",
  "common.cancel": "Cancel",

  "archiver.running": "Archiver running",
  "archiver.stopped": "Archiver stopped",
  "archiver.starting": "Starting…",
  "archiver.stopping": "Stopping…",
  "archiver.startAction": "Start",
  "archiver.stopAction": "Stop",
  "archiver.confirmStop":
    "Stop the archiver? Real-time archiving will pause until you start it again.",

  "error.alreadyRunning": "It's already running.",
  "error.notRunning": "It isn't running right now.",
  "error.archiverConnected":
    "The live archiver is currently connected. Stop it first — a backfill and the live archiver can't use the same Telegram session at the same time.",
  "error.backfillRunning":
    "A backfill is currently running. Stop it first — it and the live archiver can't use the same Telegram session at the same time.",
  "error.archiveUnattached":
    "Your archive hasn't been set up yet. Finish linking your Telegram account to start archiving.",
  "error.archiveUnavailable":
    "Your archive database is currently unavailable. Try again shortly, or contact your administrator.",
  "error.notInstanceOwner":
    "This account does not control this instance's Telegram connection.",
  "error.dbUnavailable":
    "The database is currently unavailable. Try again shortly, or contact your administrator.",

  "chatFilter.allChats": "All chats",
  "chatFilter.nChatsSelected": "{n} chats selected",
  "chatFilter.searchPlaceholder": "Search chats…",
  "chatFilter.noMatches": "No chats match your search.",

  // Auth additions
  "nav.logout": "Log out",

  "login.subtitle": "Sign in to your archive",
  "login.usernameLabel": "Username",
  "login.passwordLabel": "Password",
  "login.submit": "Sign in",
  "login.loggingIn": "Signing in…",
  "login.error": "Something went wrong. Please try again.",
  "login.registerPrompt": "Have an invite?",
  "login.registerLink": "Create an account",

  "register.subtitle": "Create your account",
  "register.inviteLabel": "Invite token",
  "register.usernameLabel": "Username",
  "register.passwordLabel": "Password",
  "register.confirmLabel": "Confirm password",
  "register.submit": "Create account",
  "register.creating": "Creating account…",
  "register.error": "Something went wrong. Please try again.",
  "register.passwordMismatch": "Passwords don't match.",
  "register.loginPrompt": "Already have an account?",
  "register.loginLink": "Sign in",

  // Settings tab (web/js/views/settings.js) - lean by design, just links out to the dedicated Telegram/archive setup page below.
  // See that page's own keys (tgSetup.*) for everything else - this used to all live under settings.*,
  // moved out once Settings became its own page-with-a-link rather than the thing housing the wizard directly (see CHANGELOG for the reasoning).
  "settings.telegramCardTitle": "Telegram connection & archive",
  "settings.telegramCardBody":
    "Manage the Telegram account TeleVault archives, and the database it's stored in.",
  "settings.telegramCardLink": "Open Telegram setup",

  // telegram-setup.html - the dedicated first-run page a new registration is sent to, also reachable any time from Settings above
  // (e.g. to relink a different account, or if an admin created the account by hand via manage_admin.py and it was never linked in the first place).
  "tgSetup.pageTitle": "Set up Telegram",
  "tgSetup.pageSubtitle":
    "Two quick steps: link the Telegram account to archive, then create the database it's stored in.",
  "tgSetup.telegramTitle": "Connect your Telegram account",
  "tgSetup.telegramStatusChecking": "Checking your Telegram link status…",
  "tgSetup.telegramIntro":
    "TeleVault archives messages using your own Telegram API credentials, not a shared app — this keeps your account's rate limits and access entirely separate from everyone else's.",
  "tgSetup.apiIdLabel": "API ID",
  "tgSetup.apiHashLabel": "API hash",
  "tgSetup.credentialsHelp":
    "From my.telegram.org — sign in there and create an app if you haven't already. Stored encrypted; never shown again after saving.",
  "tgSetup.credentialsSubmit": "Save & continue",
  "tgSetup.savingCredentials": "Saving…",
  "tgSetup.phoneLabel": "Phone number",
  "tgSetup.phonePlaceholder": "+15551234567",
  "tgSetup.phoneHelp":
    "Including country code. Not stored anywhere — used only to request this one-time verification code.",
  "tgSetup.sendCodeSubmit": "Send code",
  "tgSetup.sendingCode": "Sending…",
  "tgSetup.useDifferentCredentials": "Use different credentials",
  "tgSetup.codeLabel": "Verification code",
  "tgSetup.codeHelp": "Telegram sent a code to {phone}.",
  "tgSetup.codeSubmit": "Verify",
  "tgSetup.verifying": "Verifying…",
  "tgSetup.passwordLabel": "Telegram password (2FA)",
  "tgSetup.passwordHelp":
    "This account has two-factor authentication enabled — enter your Telegram password to finish linking.",
  "tgSetup.passwordSubmit": "Finish linking",
  "tgSetup.startOver": "Start over",
  "tgSetup.linkedBadge": "Linked",
  "tgSetup.linkedBody":
    "Your Telegram account is linked for this session. Relinking replaces the saved session with a new one.",
  "tgSetup.relinkButton": "Relink a different account",
  "tgSetup.archiveTitle": "Archive database",
  "tgSetup.archiveIntro":
    "Each account gets its own database to store archived messages, separate from every other account on this instance.",
  "tgSetup.archiveStatusChecking": "Checking archive status…",
  "tgSetup.archiveStatusReady":
    "Archive database ready — {count} messages stored.",
  "tgSetup.archiveStatusMissing": "No archive database yet.",
  "tgSetup.archiveStatusUnavailable":
    "An archive is on record but couldn't be reached just now. Try again shortly, or contact your administrator.",
  "tgSetup.provisionButton": "Create archive database",
  "tgSetup.provisioning": "Creating…",
  "tgSetup.refreshStatus": "Refresh",
  "tgSetup.allSetBody":
    "Telegram is linked and your archive database is ready. You can head to your chats now.",
  "tgSetup.continueToChats": "Continue to Chats",
  "tgSetup.skipForNow": "Skip for now — go to Chats",

  "error.invalidApiCredentials":
    "That api_id/api_hash pair isn't valid. Double-check the values from my.telegram.org.",
  "error.telegramCredentialsMissing":
    "Save your Telegram API credentials first.",
  "error.telegramInvalidPhone": "That phone number isn't valid.",
  "error.telegramFloodWait":
    "Telegram is asking us to slow down. Wait a few minutes before trying again.",
  "error.telegramNoPendingLink":
    "That verification attempt expired. Sending a new code.",
  "error.telegramTooManyAttempts": "Too many attempts. Sending a new code.",
  "error.telegramPasswordRequired":
    "This account needs its Telegram password to finish linking.",
  "error.telegramCodeRequired": "The verification code is required.",
  "error.telegramInvalidPassword": "That password wasn't correct.",
  "error.telegramInvalidCode": "That code wasn't correct.",
  "error.telegramCodeExpired": "That code expired. Sending a new one.",
  "error.archiveAlreadyProvisioned":
    "This account already has an archive database.",
  "error.provisioningPermissionDenied":
    "The database role TeleVault connects as doesn't have permission to create databases. Contact your administrator.",
};
