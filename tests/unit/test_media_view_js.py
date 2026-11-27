"""
web/js/lib/media-view.js: which box the chat view draws for a message's file, and in which language.

Runs the real module under Node (skipped when Node is absent) with the two modules that need a browser stubbed out:
i18n.js (a translator over the real en.js / uk.js tables) and auth.js (never called by these pure rendering paths).
What it cannot cover is the DOM part - blob fetching, players, lazy loading - which needs a real browser and is checked by hand.
"""

import json
import shutil
import subprocess

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

SCRIPT = """
import { renderMediaBlock, formatBytes } from "./lib/media-view.js";
import { setLang } from "./i18n.js";
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const lang of ["en", "uk"]) {
  setLang(lang);
  out[lang] = { boxes: cases.map((m) => renderMediaBlock(m)), sizes: [0, 999, 1500, 12000, 2500000, 3.2e9, -1, null].map(formatBytes) };
}
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_dir(tmp_path_factory):
    """A throw-away copy of the module with browser-only neighbours replaced, so Node can import it."""
    base = tmp_path_factory.mktemp("mediaview")
    lib = base / "lib"
    lib.mkdir()
    web = REPO_ROOT / "web" / "js"
    (lib / "media-view.js").write_text((web / "lib" / "media-view.js").read_text(encoding="utf-8"), encoding="utf-8")
    (lib / "dom.js").write_text(
        'export const escapeHtml = (v) => String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");\n',
        encoding="utf-8",
    )
    (lib / "auth.js").write_text("export async function apiFetch() { throw new Error('not used'); }\n", encoding="utf-8")
    (base / "en.mjs").write_text((web / "i18n" / "en.js").read_text(encoding="utf-8"), encoding="utf-8")
    (base / "uk.mjs").write_text((web / "i18n" / "uk.js").read_text(encoding="utf-8"), encoding="utf-8")
    (base / "i18n.js").write_text(
        'import { en } from "./en.mjs"; import { uk } from "./uk.mjs";\n'
        'let dict = en; export const setLang = (l) => { dict = l === "uk" ? uk : en; };\n'
        "export const t = (k) => dict[k] ?? `MISSING:${k}`;\n",
        encoding="utf-8",
    )
    (base / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    (base / "run.mjs").write_text(SCRIPT, encoding="utf-8")
    return base


def run(js_dir, messages):
    done = subprocess.run(["node", str(js_dir / "run.mjs"), json.dumps(messages)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=60)
    return json.loads(done.stdout)


def msg(kind="photo", status="stored", size=2_500_000, mid=12):
    return {"id": mid, "media_kind": kind, "media_status": status, "media_size": size}


def test_a_stored_file_gets_a_button_that_names_the_action_and_the_size(js_dir):
    out = run(js_dir, [msg("photo"), msg("video"), msg("voice"), msg("audio"), msg("document")])
    en = out["en"]["boxes"]
    assert 'data-media-id="12"' in en[0] and "Show photo" in en[0] and "2.5 MB" in en[0] and 'data-kind="photo"' in en[0]
    assert "Play video" in en[1] and "Play voice message" in en[2] and "Play audio" in en[3] and "Open file" in en[4]
    assert "Показати фото" in out["uk"]["boxes"][0] and "2.5 МБ" in out["uk"]["boxes"][0]


@pytest.mark.parametrize("status", ["pending", "skipped", "too_large", "disk_full", "unavailable", "failed"])
def test_a_file_that_is_not_stored_says_why_in_both_languages(js_dir, status):
    out = run(js_dir, [msg("video", status, 90_000_000)])
    for lang in ("en", "uk"):
        box = out[lang]["boxes"][0]
        assert "MISSING" not in box
        assert "cv-media--note" in box and "cv-media__open" not in box  # nothing to click: there is no file


def test_an_unavailable_file_does_not_show_a_size_and_a_refused_one_does(js_dir):
    out = run(js_dir, [msg("video", "unavailable", 90_000_000), msg("video", "too_large", 90_000_000)])["en"]["boxes"]
    assert "90 MB" not in out[0]
    assert "90 MB" in out[1]


@pytest.mark.parametrize(
    "message",
    [
        {"id": 1, "media_kind": None, "media_status": None, "media_size": None},
        {"id": 1, "media_kind": "photo", "media_status": None, "media_size": None},  # an archive that does not report the state
        {"id": 1, "media_kind": "sticker", "media_status": "stored", "media_size": 5},
        {"id": 1, "media_kind": "photo", "media_status": "banana", "media_size": 5},
    ],
)
def test_no_box_when_nothing_is_known(js_dir, message):
    assert run(js_dir, [message])["en"]["boxes"] == [""]


def test_sizes_are_decimal_and_translated(js_dir):
    out = run(js_dir, [])
    assert out["en"]["sizes"] == ["0 B", "999 B", "1.5 KB", "12 KB", "2.5 MB", "3.2 GB", "", ""]
    assert out["uk"]["sizes"][2] == "1.5 КБ"