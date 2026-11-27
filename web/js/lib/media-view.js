/**
 * Files of archived messages, shown in the chat view (step 5 of media archiving).
 *
 * Why the files are fetched by script instead of `<img src="/api/media/file/12">`:
 * the API wants the bearer token, which lives in memory only (lib/auth.js) and which the browser cannot attach to an <img>/<video> request.
 * So the file is requested with apiFetch(), turned into a blob and shown from a `blob:` URL.
 *
 * What is loaded when:
 *   - photos: automatically, once they scroll near the screen (IntersectionObserver) - a photo is small and the owner expects to see it;
 *   - video, voice, audio, documents: only after a click. A chat can hold hundreds of them, and a video is large.
 *
 * Safety: the server only labels a file as image/video/audio when its own bytes say so (utils/media_files.sniff_media_type);
 * everything else, HTML and SVG included, arrives as application/octet-stream.
 * The blob keeps that type, so anything that is not an allow-listed image/video/audio is offered as a DOWNLOAD and never opened inside the app's origin.
 *
 * The chat view redraws message lists by replacing innerHTML all the time, so nothing is wired to individual elements:
 * a MutationObserver finds new `.cv-media` boxes, a delegated click handler serves the buttons,
 * and fetched files are remembered (small LRU) so a redraw does not download them again.
 */
import { t } from "../i18n.js";
import { escapeHtml } from "./dom.js";
import { apiFetch } from "./auth.js";

/** Kinds the archive records; anything else gets no media box. */
const KINDS = ["photo", "video", "voice", "audio", "document"];

/** Statuses that explain why there is no file (a stored file has its own box). */
const WAITING_STATUSES = [
  "pending",
  "skipped",
  "too_large",
  "disk_full",
  "unavailable",
  "failed",
];

/** Fetched files kept as blob URLs (id -> {url, type, filename}).
 *  Oldest are dropped (and revoked) beyond this many. */
const CACHE_LIMIT = 80;
const cache = new Map();
/** Ids being fetched right now, so a redraw during a download does not start a second one. */
const inFlight = new Map();

/** "1.4 MB".
 *  Decimal units, like the rest of the UI and Telegram itself. */
function formatBytes(n) {
  if (typeof n !== "number" || !Number.isFinite(n) || n < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let i = 0;
  while (value >= 1000 && i < units.length - 1) {
    value /= 1000;
    i += 1;
  }
  const text =
    i === 0 || value >= 10 ? String(Math.round(value)) : value.toFixed(1);
  return `${text} ${t("media.unit." + units[i])}`;
}

/** The box a message's file goes in, or "" when the message has no file or the archive does not know its state (then the plain kind label is shown). */
function renderMediaBlock(message) {
  const kind = message.media_kind;
  const status = message.media_status;
  if (!KINDS.includes(kind) || !status) return "";
  const kindLabel = escapeHtml(t(`messages.media.${kind}`));
  const size = formatBytes(message.media_size);

  if (status === "stored") {
    const action =
      kind === "photo" ? "show" : kind === "document" ? "download" : "play";
    const label = escapeHtml(t(`media.${action}.${kind}`));
    const sizeText = size
      ? ` <span class="cv-media__size">${escapeHtml(size)}</span>`
      : "";
    return `<div class="cv-media cv-media--${kind}" data-media-id="${Number(message.id)}" data-kind="${kind}" data-state="idle">
      <button type="button" class="cv-media__open">${label}${sizeText}</button>
    </div>`;
  }
  if (!WAITING_STATUSES.includes(status)) return "";
  const why = escapeHtml(t(`media.status.${status}`));
  const sizeText =
    size && status !== "unavailable" ? ` (${escapeHtml(size)})` : "";
  return `<div class="cv-media cv-media--note" data-state="note"><span class="cv-media__kind">${kindLabel}</span> - ${why}${sizeText}</div>`;
}

function remember(id, entry) {
  cache.set(id, entry);
  while (cache.size > CACHE_LIMIT) {
    const oldest = cache.keys().next().value;
    URL.revokeObjectURL(cache.get(oldest).url);
    cache.delete(oldest);
  }
}

/** Download one file into the cache.
 *  Resolves to the entry, or null on any failure. */
function fetchFile(id) {
  if (cache.has(id)) return Promise.resolve(cache.get(id));
  if (inFlight.has(id)) return inFlight.get(id);
  const job = (async () => {
    try {
      const res = await apiFetch(`/api/media/file/${id}`);
      if (!res.ok) return null;
      const blob = await res.blob();
      const disposition = res.headers.get("Content-Disposition") || "";
      const filename =
        /filename="?([^";]+)"?/.exec(disposition)?.[1] || `televault-${id}`;
      const entry = {
        url: URL.createObjectURL(blob),
        type: blob.type || "application/octet-stream",
        filename,
      };
      remember(id, entry);
      return entry;
    } catch {
      return null;
    } finally {
      inFlight.delete(id);
    }
  })();
  inFlight.set(id, job);
  return job;
}

/** Put the fetched file into its box: an inline player for allow-listed types, a download link for everything else. */
function showEntry(box, entry) {
  const kind = box.dataset.kind;
  const url = escapeHtml(entry.url);
  const major = entry.type.split("/")[0];
  let inner;
  if (major === "image") {
    inner = `<img class="cv-media__img" src="${url}" alt="${escapeHtml(t("messages.media." + kind))}" loading="lazy">`;
  } else if (major === "video") {
    inner = `<video class="cv-media__video" src="${url}" controls preload="metadata" playsinline></video>`;
  } else if (major === "audio") {
    inner = `<audio class="cv-media__audio" src="${url}" controls preload="metadata"></audio>`;
  } else {
    inner = `<a class="cv-media__download" href="${url}" download="${escapeHtml(entry.filename)}">${escapeHtml(t("media.saveToDevice"))}</a>`;
  }
  box.dataset.state = "ready";
  box.innerHTML = inner;
}

async function load(box) {
  if (box.dataset.state === "loading" || box.dataset.state === "ready") return;
  const id = Number(box.dataset.mediaId);
  const previous = box.innerHTML;
  box.dataset.state = "loading";
  box.innerHTML = `<span class="cv-media__status">${escapeHtml(t("media.loading"))}</span>`;
  const entry = await fetchFile(id);
  // The list may have been redrawn while waiting; then this box is detached and the new one picks the cached file up itself.
  if (!box.isConnected) return;
  if (!entry) {
    box.dataset.state = "idle";
    box.innerHTML = previous;
    box.insertAdjacentHTML(
      "beforeend",
      `<span class="cv-media__status cv-media__status--error" role="alert">${escapeHtml(t("media.loadFailed"))}</span>`,
    );
    return;
  }
  showEntry(box, entry);
}

let photoObserver = null;

/** Fill every idle box that has its file cached already; queue photos for lazy loading. */
function hydrate(scope) {
  scope.querySelectorAll?.('.cv-media[data-state="idle"]').forEach((box) => {
    const cached = cache.get(Number(box.dataset.mediaId));
    if (cached) showEntry(box, cached);
    else if (box.dataset.kind === "photo" && photoObserver)
      photoObserver.observe(box);
  });
}

/** Start watching `root` (the chat view's pane).
 *  Call once; safe to call again for the same root. */
function initMediaView(root) {
  if (!root || root.dataset.mediaWired === "1") return;
  root.dataset.mediaWired = "1";

  if (typeof IntersectionObserver === "function") {
    photoObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          photoObserver.unobserve(entry.target);
          load(entry.target);
        }
      },
      { rootMargin: "300px 0px" },
    );
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest?.(".cv-media__open");
    if (button) load(button.closest(".cv-media"));
  });

  new MutationObserver((records) => {
    for (const record of records) {
      record.addedNodes.forEach((node) => {
        if (node.nodeType !== 1) return;
        if (node.matches?.(".cv-media"))
          hydrate({ querySelectorAll: () => [node] });
        else hydrate(node);
      });
    }
  }).observe(root, { childList: true, subtree: true });

  hydrate(root);
}

export { renderMediaBlock, initMediaView, formatBytes };
