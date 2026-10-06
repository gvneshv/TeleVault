/**
 * App shell controller.
 *
 * Scope of this file, deliberately:
 * switching the visible section when a nav link is clicked, and delegating each view's lazy initialization - nothing else.
 * Views themselves own their fetching/rendering logic.
 *
 * This is the single ES module entry point for the app (see index.html's one <script type="module" src="/js/app.js">).
 * It imports every view module, which is what actually causes their code to run at all - an unimported ES module's top-level code
 * (e.g. chats.js's own DOMContentLoaded listener) never executes, unlike a classic <script> tag which always runs once loaded.
 * chats.js is imported for that side effect only (it self-initializes as the landing view);
 * the other five export an init() called lazily below, the first time their tab is opened.
 * The Admin tab is the exception to both:
 * its nav button, its section, AND its module (views/admin.js, loaded via dynamic import()) only come into existence for the admin account - see mountAdminTab() below.
 *
 * No client-side router or URL hash handling yet.
 * Adding one is a reasonable future step once there are per-item views (e.g. a single chat or message) that benefit from being linkable/bookmarkable
 * - not needed for the nav-only shell.
 */

import "./views/chats.js";
import "./archiver-toggle.js";
import { initMessagesView } from "./views/messages.js";
import { initDeletedView } from "./views/deleted.js";
import { initStatsView } from "./views/stats.js";
import { initHealthView } from "./views/health.js";
import { initBackfillView } from "./views/backfill.js";
import { initSettingsView } from "./views/settings.js";
import { t } from "./i18n.js";
import { logout, isAdmin, fetchCurrentUser } from "./lib/auth.js";

document.addEventListener("DOMContentLoaded", () => {
  const navList = document.querySelector(".app-nav__list");

  function showView(viewName) {
    // Queried on every call (not once up front) because the Admin tab's elements are added/removed after load.
    const links = document.querySelectorAll(".app-nav__link[data-view]");
    const views = document.querySelectorAll(".app-view");
    views.forEach((view) => {
      view.hidden = view.dataset.view !== viewName;
    });
    links.forEach((link) => {
      if (link.dataset.view === viewName) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });

    // Views other than the landing "chats" tab don't self-initialize on DOMContentLoaded (no point fetching data for a hidden tab).
    // Each exports an init() that's safe to call more than once - the view itself guards against re-initializing.
    if (viewName === "messages") initMessagesView();
    if (viewName === "deleted") initDeletedView();
    if (viewName === "stats") initStatsView();
    if (viewName === "health") initHealthView();
    if (viewName === "backfill") initBackfillView();
    if (viewName === "settings") initSettingsView();
    if (viewName === "admin") {
      // Dynamic import: a non-admin's browser never even requests this file through the app.
      import("./views/admin.js")
        .then((m) => m.initAdminView())
        .catch((err) => console.error("Failed to load admin view:", err));
    }
  }

  // Delegated, so it also covers nav buttons added later (the Admin tab).
  navList.addEventListener("click", (event) => {
    const link = event.target.closest(".app-nav__link[data-view]");
    if (link) showView(link.dataset.view);
  });

  /**
   * Create the Admin nav button + view section, only for the admin account.
   *
   * What this buys: a non-admin's DOM/Elements panel contains no Admin tab, no form, nothing to poke at.
   * What it does NOT buy, honestly: the admin.js file and its strings are still ordinary static files anyone can request by URL,
   * and nothing here is an access check (isAdmin() reads an unverified token claim - see lib/auth.js).
   * That's fine because the code isn't a secret: every /api/admin/* route re-verifies the admin on every request (api.dependencies.require_admin),
   * and knowing an endpoint exists doesn't help guess a password or token.
   */
  function mountAdminTab() {
    if (document.getElementById("admin-nav-link")) return;
    const main = document.querySelector(".app-main");
    if (!main) return;

    const item = document.createElement("li");
    const button = document.createElement("button");
    button.id = "admin-nav-link";
    button.className = "app-nav__link";
    button.dataset.view = "admin";
    const label = document.createElement("span");
    label.setAttribute("data-i18n", "nav.admin");
    label.textContent = t("nav.admin");
    button.append(label);
    item.append(button);
    navList.append(item);

    const section = document.createElement("section");
    section.className = "app-view";
    section.dataset.view = "admin";
    section.hidden = true;
    const heading = document.createElement("h1");
    heading.setAttribute("data-i18n", "nav.admin");
    heading.textContent = t("nav.admin");
    const root = document.createElement("div");
    root.id = "admin-root";
    section.append(heading, root);
    main.append(section);
  }

  function unmountAdminTab() {
    const button = document.getElementById("admin-nav-link");
    if (!button) return;
    const wasOpen = button.getAttribute("aria-current") === "page";
    button.closest("li")?.remove();
    document.querySelector('.app-view[data-view="admin"]')?.remove();
    if (wasOpen) showView("chats");
  }

  // Default view on load.
  showView("chats");

  // Logout button lives in the nav rail (see #logout-button in index.html),
  // not inside a .app-view section - reachable regardless of which tab is open, same reasoning as #archiver-toggle above it.
  const logoutButton = document.getElementById("logout-button");
  if (logoutButton) {
    logoutButton.addEventListener("click", async () => {
      logoutButton.disabled = true;
      await logout(); // clears the session and redirects to /login.html itself - see lib/auth.js
    });
  }

  // "admin" badge next to the wordmark, and the Admin nav tab itself
  // (see lib/auth.js's isAdmin() for why this is UI-only, never an access check - api.dependencies.require_admin is the real gate).
  // Driven by the "televault:authchange" event (also lib/auth.js) rather than checked once here,
  // since accessToken isn't populated yet at DOMContentLoaded time - refresh-on-load resolves asynchronously
  // (chats.js/archiver-toggle.js each kick it off independently;
  // see this file's own module docstring on why there's no shared init).
  const adminBadge = document.getElementById("admin-badge");
  // Current username, shown under the wordmark - deliberately plain text, not a link or button
  // (the person asked for a quiet reminder of who they're logged in as while testing multiple accounts, not another control).
  // Needs its own fetchCurrentUser() call (GET /auth/me) rather than a JWT claim like is_admin above:
  // the access token never carried a username claim and adding one would mean re-issuing every token shape -
  // one extra request on login is simpler and has no security implications either way.
  const usernameEl = document.getElementById("current-username");
  document.addEventListener("televault:authchange", async (event) => {
    const admin = isAdmin();
    if (adminBadge) adminBadge.hidden = !admin;
    if (admin) mountAdminTab();
    else unmountAdminTab();

    if (usernameEl) {
      if (event.detail.state === "in") {
        const me = await fetchCurrentUser();
        usernameEl.textContent = me ? me.username : "";
      } else {
        usernameEl.textContent = "";
      }
    }
  });
});
