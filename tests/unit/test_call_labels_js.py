"""
web/js/lib/call-labels.js must understand every label handlers/helpers.format_call_text() can produce, in both interface languages.

The archive stores call labels in English; the web UI translates them at display time.
If the Python side gains a new label shape and the JS pattern is not updated, that label would silently stay English in the Ukrainian UI - this test is what notices.
It runs the real JS under Node (skipped when Node is absent).
"""

import json
import shutil
import subprocess

import pytest
from telethon.tl.types import (
    MessageActionPhoneCall,
    PhoneCallDiscardReasonBusy,
    PhoneCallDiscardReasonDisconnect,
    PhoneCallDiscardReasonHangup,
    PhoneCallDiscardReasonMissed,
)

from handlers.helpers import format_call_text
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

SCRIPT = """
import { localizeCallLabel } from "%(lib)s";
import { en } from "%(en)s";
import { uk } from "%(uk)s";
const labels = JSON.parse(process.argv[1]);
const tr = (dict) => (key) => dict[key] ?? `MISSING:${key}`;
process.stdout.write(JSON.stringify({
  en: labels.map((l) => localizeCallLabel(l, tr(en))),
  uk: labels.map((l) => localizeCallLabel(l, tr(uk))),
}));
"""


def run_js(labels: list[str]) -> dict[str, list[str]]:
    web = REPO_ROOT / "web" / "js"
    script = SCRIPT % {
        "lib": (web / "lib" / "call-labels.js").as_uri(),
        "en": (web / "i18n" / "en.js").as_uri(),
        "uk": (web / "i18n" / "uk.js").as_uri(),
    }
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(labels)], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(out.stdout)


def every_label() -> list[str]:
    reasons = [None, PhoneCallDiscardReasonMissed(), PhoneCallDiscardReasonBusy(), PhoneCallDiscardReasonDisconnect(), PhoneCallDiscardReasonHangup(), object()]
    labels = set()
    for reason in reasons:
        for duration in (None, 0, 3, 59, 60, 65, 272, 3725):
            for video in (False, True):
                labels.add(format_call_text(MessageActionPhoneCall(call_id=1, duration=duration, reason=reason, video=video)))
    return sorted(labels)


class TestLocalizeCallLabel:
    def test_the_python_side_produces_the_shapes_the_test_expects(self):
        labels = every_label()
        assert "[Missed call]" in labels and "[Voice call · 1 min 05 sec]" in labels and "[Video call · 4 min 32 sec]" in labels
        assert len(labels) >= 12

    def test_english_shows_exactly_what_is_stored(self):
        labels = every_label()
        assert run_js(labels)["en"] == labels

    def test_ukrainian_translates_every_label_completely(self):
        labels = every_label()
        translated = run_js(labels)["uk"]
        for stored, shown in zip(labels, translated):
            assert shown != stored, stored
            assert "MISSING" not in shown, stored
            assert not any(word in shown for word in ("call", "Call", "sec", "min", "outcome", "disconnected")), (stored, shown)

    def test_a_known_label_reads_naturally(self):
        assert run_js(["[Voice call · 1 min 05 sec]", "[Video call · 30 sec (disconnected)]"])["uk"] == [
            "[Голосовий дзвінок · 1 хв 05 с]",
            "[Відеодзвінок · 30 с (розрив зʼєднання)]",
        ]

    @pytest.mark.parametrize("text", ["hello", "[Voice call]", "[voice call · 5 sec]", "Voice call · 5 sec", "[Voice call · 5 sec] extra", "", "[Missed call] now"])
    def test_anything_else_is_left_exactly_as_it_is(self, text):
        assert run_js([text])["uk"] == [text]