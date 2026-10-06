/**
 * In-memory access token, login/register/logout API calls, and apiFetch() -
 * the one thing every other view module on the authenticated app page (index.html) needs from this file,
 * since it's a drop-in replacement for fetch() against endpoints gated by get_archive_connection / require_instance_owner / get_current_user (api/dependencies.py):
 * /api/chats, /messages, /deleted, /stats, /health, /telethon/*, /backfill/*.
 *
 * PAGE SPLIT: login.html, register.html, and index.html (the app itself) are three separate pages,
 * not one page showing/hiding a login form - see login.js/register.js for the two standalone auth pages.
 * This file is shared by all three (login()/register()/hasActiveSession() for the first two, apiFetch()/logout() for the app page),
 * but only index.html's views actually call apiFetch().
 *
 * TOKEN STORAGE - a real fix, not the earlier tradeoff-and-accept approach:
 *   The refresh token NEVER reaches this file, or any JavaScript, at all.
 *   It travels exclusively as an httpOnly cookie the browser manages on its own (see api/routes/auth.py's module docstring) -
 *   there is nothing here for an XSS payload to read that would give it a 30-day-lived credential.
 *   The access token lives in the `accessToken` module-scoped variable below and NOWHERE else -
 *   not localStorage, not sessionStorage, not a DOM attribute.
 *   It's still technically readable by injected JS while the page is open,
 *   but it vanishes on reload/tab-close and is short-lived (15 minutes) even if captured -
 *   a much smaller exposure than the previous 30-day localStorage refresh token.
 *
 * CONSEQUENCE: a page reload always loses the in-memory access token.
 * There is no synchronous "am I logged in" check anymore (there was, briefly, against localStorage - see this repo's history) -
 * the httpOnly cookie can only be verified by actually asking the server, via POST /auth/refresh.
 * apiFetch() below folds that into its normal flow:
 * if there's no in-memory access token yet, it tries a refresh first, exactly the same way it reacts to a 401 on an existing token.
 * The very first apiFetch() call made by whichever view happens to self-initialize first
 * (chats.js, archiver-toggle.js) IS the login check for index.html;
 * if it fails, apiFetch() redirects to /login.html itself (see redirectToLogin() below) rather than trying to show anything in place -
 * there's no login form embedded in index.html to fall back to.
 *
 * data-auth on <html> ("checking" | "in" | "out") is what base.css uses to show/hide .auth-loading vs .app-shell on index.html,
 * and - with the opposite mapping - .auth-loading vs .auth-page on login.html/register.html:
 * those two pages start at "checking" (hiding the form) and flip to "out" once hasActiveSession() resolves false (see login.js/register.js),
 * revealing the form only after confirming there's nothing to redirect for.
 * They never reach "in" themselves - hasActiveSession() resolving true means an immediate window.location redirect away from the page instead.
 */

/** @type {string | null} */
let accessToken = null;

/**
 * Coalesces concurrent refresh attempts into one in-flight request.
 *
 * Why this matters here specifically:
 * chats.js AND archiver-toggle.js both self-initialize on DOMContentLoaded, independently of each other,
 * and BOTH will find accessToken === null on a fresh page load and want to refresh.
 * Without this lock, two near-simultaneous POST /auth/refresh calls would both present the SAME (not-yet-rotated) cookie -
 * the first to reach the server rotates it and succeeds;
 * the second, arriving a moment later with what is now a stale, already-rotated refresh token,
 * would trip the reuse-detection path in api/routes/auth.py's refresh() and revoke EVERY session for the account,
 * including the one the first request just created.
 * A page load would log itself out.
 * Sharing one in-flight promise across every concurrent caller closes that race entirely.
 *
 * @type {Promise<boolean> | null}
 */
let refreshPromise = null;

function setAuthState(state) {
  document.documentElement.setAttribute("data-auth", state);
  // Lets app.js / telegram-setup.js
  // (separate entry points, each with their own DOMContentLoaded flow - see this file's own module docstring on why there's no shared init)
  // react to auth resolving without polling document.documentElement or duplicating the refresh-on-load dance themselves.
  // Fired on every state, not just "in", so a listener can also react to "out" (e.g. hide something shown optimistically).
  document.dispatchEvent(
    new CustomEvent("televault:authchange", { detail: { state } }),
  );
}

/**
 * Decode the CURRENT accessToken's payload, without verifying the signature.
 * UI-only, same caveat for every caller of this (isAdmin()/getCurrentUserId() below): this must NEVER be treated as an access check or as trustworthy identity.
 * Every protected endpoint re-verifies the token server-side on every request (see utils/security.py's create_access_token()/decode_access_token());
 * nothing here could substitute for that even if it were wrong, tampered with, or simply stale relative to a change another session just made.
 * Returns null (not throws) for a missing/malformed token.
 */
function decodeAccessTokenClaims() {
  if (!accessToken) return null;
  try {
    const payload = accessToken.split(".")[1];
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

/** UI-only convenience (e.g. showing an "admin" badge next to the wordmark) - see decodeAccessTokenClaims()'s own docstring. */
function isAdmin() {
  return Boolean(decodeAccessTokenClaims()?.is_admin);
}

/**
 * UI-only convenience (e.g. telling "is this row me?" apart in the admin user list, web/js/views/admin.js) - see decodeAccessTokenClaims()'s own docstring.
 * Returns null rather than a number for "don't know" so callers can't mistake it for a real,
 * falsy-but-valid id (user id 0 can't occur here since Postgres Identity columns start at 1, but null is still the honest "no claim decoded" value regardless).
 */
function getCurrentUserId() {
  const claims = decodeAccessTokenClaims();
  return claims && typeof claims.user_id === "number" ? claims.user_id : null;
}

/**
 * Fetch and cache GET /auth/me's response (username, is_admin, etc. - see api/schemas/auth.py's UserOut) for this page load.
 * Cached rather than re-fetched on every call: callers (app.js, telegram-setup.js) want this once,
 * right after auth resolves, to show the signed-in username next to the wordmark - not a live profile view that needs to track in-session changes
 * (a username change, were one ever added, would need its own cache-busting at that point, not before).
 * Returns null on any failure (network, 401 that redirectToLogin() already handles via apiFetch()) - callers treat that the same as "nothing to show", not a crash.
 */
let cachedMe = null;

async function fetchCurrentUser() {
  if (cachedMe) return cachedMe;
  try {
    const res = await apiFetch("/api/auth/me");
    if (!res.ok) return null;
    cachedMe = await res.json();
    return cachedMe;
  } catch {
    return null;
  }
}

function redirectToLogin() {
  window.location.href = "/login.html";
}

/**
 * Turn a response body's "detail" into the message string for httpError().
 *
 * FastAPI sends a plain string for errors the routes raise themselves ("That username is already taken."),
 * but an ARRAY of objects for request-validation failures (HTTP 422 - e.g. a too-short password), which `new Error(array)` stringifies to "[object Object]".
 * For anything that isn't a string, return "" so the calling page falls back to its own localized text
 * (login.js/register.js: `err.message || t(...)`) instead of showing raw JSON or a bare status code.
 * @param {unknown} detail
 * @param {number} status
 * @returns {string}
 */
function errorMessageFrom(detail, status) {
  if (typeof detail === "string" && detail) return detail;
  if (detail) return ""; // structured (validation) detail - not user-presentable as-is
  return `HTTP ${status}`;
}

/**
 * Error for a failed login()/register() request, carrying the HTTP status as `.status` alongside the usual `.message`.
 * The pages show the message as-is; the status exists so they can tell failure KINDS apart
 * (e.g. login.js clears the password field only on 401 - wrong credentials - not on 403 "locked" or 429 "too many attempts", where the password was fine).
 * @param {string} message
 * @param {number} status
 * @returns {Error & { status: number }}
 */
function httpError(message, status) {
  return Object.assign(new Error(message), { status });
}

/**
 * POST /auth/login.
 * Throws an Error with a user-facing message
 * (the backend's own `detail` text, already written to be shown directly - see api/routes/auth.py's login()) on failure;
 * the login view catches this and displays it.
 * Stores the access token in memory and flips data-auth on success;
 * the refresh token arrives as a Set-Cookie header this code never touches directly.
 */
async function login(username, password) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw httpError(errorMessageFrom(body.detail, res.status), res.status);
  }
  accessToken = body.access_token;
  setAuthState("in");
}

/**
 * POST /auth/register.
 * Same error-throwing contract as login() above (err.message, plus err.status via httpError()) - register.js shows err.message directly.
 * Also logs the new account straight in (see api/routes/auth.py's register() for why:
 * proving a valid invite token AND choosing a password in one request already establishes everything a follow-up login would check).
 */
async function register(inviteToken, username, password) {
  const res = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken, username, password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw httpError(errorMessageFrom(body.detail, res.status), res.status);
  }
  accessToken = body.access_token;
  setAuthState("in");
}

/**
 * Used only by login.html/register.html:
 * is there already a valid session (a still-good refresh cookie),
 * so those pages can redirect straight to the app instead of showing the form to someone who's already signed in?
 * Shares the same coalesced tryRefresh() as apiFetch() - not a separate request path.
 * @returns {Promise<boolean>}
 */
async function hasActiveSession() {
  return tryRefresh();
}

/**
 * POST /auth/logout (best-effort - proceeds with local cleanup even if the request fails),
 * then clears the in-memory access token and redirects to /login.html.
 * The httpOnly cookie itself is cleared server-side by the response's Set-Cookie (Max-Age=0) -
 * nothing for this code to clear on its side beyond the in-memory token.
 */
async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } catch {
    // Best-effort - if the network request fails, the refresh token simply sits unrevoked server-side until it expires on its own.
    // Not worth blocking the user's own logout on a flaky connection to enforce that.
  }
  accessToken = null;
  cachedMe = null;
  redirectToLogin();
}

/**
 * Attempt one refresh, coalescing concurrent callers (see refreshPromise's own docstring above).
 * @returns {Promise<boolean>} whether the refresh succeeded (accessToken is updated as a side effect).
 */
function tryRefresh() {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const res = await fetch("/api/auth/refresh", { method: "POST" });
        if (!res.ok) return false;
        const body = await res.json();
        accessToken = body.access_token;
        setAuthState("in");
        return true;
      } catch {
        return false;
      }
    })().finally(() => {
      refreshPromise = null;
    });
  }
  return refreshPromise;
}

/**
 * Drop-in replacement for fetch() against endpoints that require an access token.
 *
 * If there's no in-memory access token yet (fresh page load, or a previous refresh failed),
 * attempts one refresh before the real request - this is what makes a page reload transparently "just work" when the httpOnly cookie is still valid,
 * without any separate bootstrap step (see this module's own docstring).
 *
 * On a 401 from the real request (the access token expired mid-session), refreshes once and retries.
 * Deliberately does NOT retry on a 403: that status means the caller IS a valid, authenticated account,
 * just not the one require_owner-family dependencies allow for this resource
 * (see api/dependencies.py's get_archive_connection() / require_instance_owner()) -
 * a fresh access token changes nothing about which account it belongs to.
 *
 * If there's no way to get a valid session (refresh itself fails), redirects to /login.html (see redirectToLogin() above)
 * and returns the failed Response rather than throwing -
 * every existing view's `if (!res.ok)` handling keeps working unchanged for the brief instant before the redirect takes effect.
 *
 * @param {string} input
 * @param {RequestInit} [init]
 * @returns {Promise<Response>}
 */
async function apiFetch(input, init = {}) {
  if (!accessToken) {
    const refreshed = await tryRefresh();
    if (!refreshed) {
      redirectToLogin();
      return new Response(null, {
        status: 401,
        statusText: "Not authenticated",
      });
    }
  }

  const withAuth = () => ({
    ...init,
    headers: {
      ...(init.headers || {}),
      Authorization: `Bearer ${accessToken}`,
    },
  });

  const res = await fetch(input, withAuth());
  if (res.status !== 401) return res;

  const refreshed = await tryRefresh();
  if (!refreshed) {
    redirectToLogin();
    return res;
  }
  return fetch(input, withAuth());
}

export {
  login,
  register,
  logout,
  hasActiveSession,
  apiFetch,
  setAuthState,
  isAdmin,
  getCurrentUserId,
  fetchCurrentUser,
};
