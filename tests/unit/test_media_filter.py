"""
handlers/media_filter.py: which media TeleVault stores, and as what kind.

Real Telethon types are used (not look-alike fakes) so a Telethon upgrade that renames an attribute breaks here, not silently in production.
"""

import pytest
from sqlalchemy import CheckConstraint
from telethon.tl import types

from handlers.media_filter import KINDS, media_kind, media_size


def doc(*attributes, size=1000):
    return types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="application/octet-stream",
        size=size, dc_id=1, attributes=list(attributes),
    )


def message(media):
    return types.Message(id=1, peer_id=types.PeerUser(1), date=None, message="", media=media)


def document_message(*attributes, size=1000):
    return message(types.MessageMediaDocument(document=doc(*attributes, size=size)))


FILENAME = types.DocumentAttributeFilename(file_name="a.bin")
VIDEO = types.DocumentAttributeVideo(duration=1, w=1, h=1)
ROUND_VIDEO = types.DocumentAttributeVideo(duration=1, w=1, h=1, round_message=True)
VOICE = types.DocumentAttributeAudio(duration=1, voice=True)
MUSIC = types.DocumentAttributeAudio(duration=1, title="song")
ANIMATED = types.DocumentAttributeAnimated()
STICKER = types.DocumentAttributeSticker(alt="x", stickerset=types.InputStickerSetEmpty())
CUSTOM_EMOJI = types.DocumentAttributeCustomEmoji(alt="x", stickerset=types.InputStickerSetEmpty())


@pytest.mark.parametrize(
    "msg, expected",
    [
        (message(types.MessageMediaPhoto(photo=types.Photo(
            id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))), "photo"),
        (document_message(FILENAME), "document"),
        (document_message(VIDEO), "video"),
        (document_message(ROUND_VIDEO), "video"),
        (document_message(VOICE), "voice"),
        (document_message(MUSIC), "audio"),
        # Not stored: GIF (a GIF also carries a video attribute - "animated" must win), stickers, custom emoji.
        (document_message(VIDEO, ANIMATED), None),
        (document_message(ANIMATED), None),
        (document_message(STICKER), None),
        (document_message(CUSTOM_EMOJI), None),
        # Nothing to classify or fetch.
        (message(types.MessageMediaPhoto(photo=None)), None),
        (message(types.MessageMediaDocument(document=None)), None),
        (document_message(), None),
        (message(None), None),
        (message(types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))), None),
    ],
)
def test_kind(msg, expected):
    assert media_kind(msg) == expected


def test_an_object_without_media_attributes_is_not_media():
    assert media_kind(object()) is None


def test_every_kind_it_can_return_is_one_the_database_accepts():
    """db/schema.py's CHECK lists the same words; a kind missing there would fail at insert time."""
    from db.schema import message_media

    check = next(c for c in message_media.constraints if isinstance(c, CheckConstraint) and c.name == "ck_message_media_kind")
    expression = str(check.sqltext)
    for kind in KINDS:
        assert f"'{kind}'" in expression
    assert len(KINDS) == expression.count("'") // 2


@pytest.mark.parametrize("size, expected", [(0, 0), (123, 123), (None, None)])
def test_size_is_what_telegram_declared(size, expected):
    msg = document_message(FILENAME, size=size)
    assert media_size(msg) == expected


def photo_message(*sizes):
    photo = types.Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=list(sizes), dc_id=1)
    return message(types.MessageMediaPhoto(photo=photo))


def test_an_empty_photo_has_no_size():
    assert media_size(message(types.MessageMediaPhoto(photo=None))) is None
    assert media_size(photo_message()) is None


def test_a_photo_is_measured_by_its_heaviest_rendition():
    """That is the rendition download_media fetches, so it is the size the disk limit must be checked against BEFORE downloading."""
    msg = photo_message(
        types.PhotoStrippedSize(type="i", bytes=b"\x01\x28\x28abc"),
        types.PhotoSize(type="m", w=320, h=240, size=15_000),
        types.PhotoSizeProgressive(type="y", w=1280, h=960, sizes=[9_000, 80_000, 210_000]),
        types.PhotoSize(type="x", w=800, h=600, size=120_000),
    )
    assert media_size(msg) == 210_000


def test_a_cached_photo_rendition_counts_by_its_bytes():
    assert media_size(photo_message(types.PhotoCachedSize(type="m", w=1, h=1, bytes=b"x" * 321))) == 321


def test_a_photo_with_only_a_stripped_preview_has_no_size():
    """The stripped preview is never what we download, so it must not pass for the file's size."""
    assert media_size(photo_message(types.PhotoStrippedSize(type="i", bytes=b"abc"))) is None