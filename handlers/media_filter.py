"""
Media classification: "is this message's file something TeleVault stores, and what kind is it?"

What it is
    A pure function, media_kind(), that looks only at the metadata a Telethon message already carries and answers with one of KINDS
    (the media the owner chose to archive) or None (not stored).
    It never touches the database, the network or the disk, so it is safe to call anywhere, before anything else, exactly like handlers/ingest_filter.is_self_destructing().

What is stored (owner's decision, 2026-11-18)
    photo     - a photo.
    video     - a video file, including round "video messages".
    voice     - a voice message.
    audio     - music and other audio files.
    document  - any other file (PDF, archive, ...).

What is deliberately NOT stored
    GIFs (a document with Telegram's "animated" attribute), stickers and animated emoji.
    GIFs are heavy and behave like reactions rather than content; a "store GIFs" setting may be added later (off by default).
    Stickers and custom emoji are decoration, not files the owner would want to keep.
    Self-destructing / view-once media is excluded earlier, by handlers/ingest_filter.is_self_destructing(); this module does not repeat that check.
    Link previews (MessageMediaWebPage), polls, locations, contacts and the like are not files and are not stored either.

Telethon facts this relies on (checked against the installed Telethon; re-check after an upgrade)
    - A file is MessageMediaDocument whose `.document` carries `.attributes`; a photo is MessageMediaPhoto.
    - "Animated" is DocumentAttributeAnimated, a sticker is DocumentAttributeSticker, a custom emoji is DocumentAttributeCustomEmoji.
    - A voice message is DocumentAttributeAudio(voice=True); a round video has DocumentAttributeVideo(round_message=True) and counts as video.
    A document that has not been loaded (the Telegram "empty" document) has no attributes and is treated as not storable.

Order of the checks matters: a GIF also has a video attribute, so "animated" must be tested before "video".
"""

from typing import Any

from telethon.tl.types import (
    DocumentAttributeAnimated,
    DocumentAttributeAudio,
    DocumentAttributeCustomEmoji,
    DocumentAttributeSticker,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

# The kinds that are stored.
# The same values are the CHECK constraint on message_media.kind (db/schema.py) - keep the two in step.
KINDS = ("photo", "video", "voice", "audio", "document")


def media_kind(message: Any) -> str | None:
    """
    The stored kind of `message`'s media (one of KINDS), or None if the message has no media or it is not something TeleVault stores.

    Reads attributes only; objects that lack them (older Telethon objects, test fakes) simply yield None.
    """
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaPhoto):
        # An "empty" photo (Telegram could not provide it) has no .photo; there is nothing to download.
        return "photo" if getattr(media, "photo", None) is not None else None
    if not isinstance(media, MessageMediaDocument):
        return None

    document = getattr(media, "document", None)
    attributes = getattr(document, "attributes", None)
    if not attributes:
        # No document, or a document with no attributes at all: not a file we can classify or fetch.
        return None

    # GIFs, stickers and custom emoji first: a GIF also carries a video attribute and would otherwise be taken for a video.
    if any(isinstance(a, (DocumentAttributeAnimated, DocumentAttributeSticker, DocumentAttributeCustomEmoji)) for a in attributes):
        return None

    for attribute in attributes:
        if isinstance(attribute, DocumentAttributeAudio):
            return "voice" if attribute.voice else "audio"
    if any(isinstance(a, DocumentAttributeVideo) for a in attributes):
        return "video"
    return "document"


def media_size(message: Any) -> int | None:
    """
    The file's size in bytes as Telegram declares it, or None when it is not known (a photo's size is only known after choosing a size to fetch).

    Used to refuse an over-limit file BEFORE downloading a single byte of it, so the size cap costs no traffic.
    """
    document = getattr(getattr(message, "media", None), "document", None)
    size = getattr(document, "size", None)
    return size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None