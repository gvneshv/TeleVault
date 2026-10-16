"""handlers/helpers.py - the shared rules for what gets archived and how chats/senders are described."""

import logging

import pytest
from telethon import types
from telethon.tl.types import (
    MessageActionPhoneCall,
    PhoneCallDiscardReasonBusy,
    PhoneCallDiscardReasonDisconnect,
    PhoneCallDiscardReasonHangup,
    PhoneCallDiscardReasonMissed,
)

from handlers.helpers import _format_duration, format_call_text, get_chat_type, get_sender_fields, resolve_message_text


def _bare(cls, **attrs):
    """Build a Telethon TL object without its (long, version-dependent) constructor - isinstance() checks only need the class."""
    obj = cls.__new__(cls)
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


class TestGetChatType:
    def test_user_is_private(self):
        assert get_chat_type(_bare(types.User)) == "private"

    def test_basic_group(self):
        assert get_chat_type(_bare(types.Chat)) == "group"

    def test_megagroup_is_supergroup(self):
        assert get_chat_type(_bare(types.Channel, megagroup=True)) == "supergroup"

    def test_broadcast_channel(self):
        assert get_chat_type(_bare(types.Channel, megagroup=False)) == "channel"

    def test_unknown_type_falls_back_to_group_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="handlers.helpers"):
            assert get_chat_type(object()) == "group"
        assert "Unknown chat entity type" in caplog.text

    def test_result_is_always_a_value_the_schema_accepts(self):
        allowed = {"private", "group", "supergroup", "channel"}
        for entity in (_bare(types.User), _bare(types.Chat), _bare(types.Channel, megagroup=True), _bare(types.Channel, megagroup=False), object()):
            assert get_chat_type(entity) in allowed


class TestGetSenderFields:
    def test_none_sender_gives_three_nones(self):
        assert get_sender_fields(None) == (None, None, None)

    def test_reads_the_three_fields(self):
        sender = _bare(types.User, username="alice", first_name="Alice", last_name="Liddell")
        assert get_sender_fields(sender) == ("alice", "Alice", "Liddell")

    def test_missing_attributes_become_none(self):
        assert get_sender_fields(object()) == (None, None, None)


class TestResolveMessageText:
    def test_plain_text_is_returned_as_is(self):
        msg = type("M", (), {"message": "hello", "action": None})()
        assert resolve_message_text(msg) == "hello"

    def test_media_only_message_has_no_text(self):
        msg = type("M", (), {"message": "", "action": None})()
        assert not resolve_message_text(msg)

    def test_phone_call_service_message_gets_a_synthetic_label(self):
        action = MessageActionPhoneCall(call_id=1, duration=65, reason=PhoneCallDiscardReasonHangup(), video=False)
        msg = type("M", (), {"message": "", "action": action})()
        assert resolve_message_text(msg) == "[Voice call · 1 min 05 sec]"


class TestFormatCallText:
    @staticmethod
    def _call(reason=None, duration=None, video=False):
        return MessageActionPhoneCall(call_id=1, duration=duration, reason=reason, video=video)

    def test_missed(self):
        assert format_call_text(self._call(PhoneCallDiscardReasonMissed())) == "[Missed call]"

    def test_busy_is_reported_as_declined(self):
        assert format_call_text(self._call(PhoneCallDiscardReasonBusy())) == "[Declined call]"

    def test_disconnected_with_duration(self):
        text = format_call_text(self._call(PhoneCallDiscardReasonDisconnect(), duration=30))
        assert text == "[Voice call · 30 sec (disconnected)]"

    def test_disconnected_without_duration(self):
        assert format_call_text(self._call(PhoneCallDiscardReasonDisconnect())) == "[Call ended (disconnected)]"

    def test_hangup_with_duration_video(self):
        text = format_call_text(self._call(PhoneCallDiscardReasonHangup(), duration=272, video=True))
        assert text == "[Video call · 4 min 32 sec]"

    def test_no_reason_and_no_duration_means_never_answered(self):
        assert format_call_text(self._call(None)) == "[Missed call]"

    def test_unknown_reason_object_is_reported_not_crashed(self):
        assert format_call_text(self._call(object(), duration=5)) == "[Voice call · unknown outcome]"


@pytest.mark.parametrize(
    "seconds, expected",
    [(0, "0 sec"), (3, "3 sec"), (59, "59 sec"), (60, "1 min 00 sec"), (65, "1 min 05 sec"), (120, "2 min 00 sec"), (3725, "62 min 05 sec")],
)
def test_format_duration(seconds, expected):
    assert _format_duration(seconds) == expected
