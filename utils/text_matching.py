"""
How a message ignore rule (db/schema.py's ingest_ignore_rules) is compared with a message's text.

The rule is a PLAIN SUBSTRING, ignoring case: a message is ignored if its text contains the phrase anywhere.
It is deliberately not a regular expression - a badly written one can hang the archiver (catastrophic backtracking) on every incoming message,
and defending against that is far more machinery than a list of phrases needs.
Regex can be added later without breaking stored rules.

Both sides go through fold() so the comparison is the same for the stored rule and for the incoming text:
  - NFC normalization, because the same visible letter can be one code point or a base letter plus a combining mark
    (Cyrillic "й" is the usual example), depending on which app produced the text;
  - casefold(), the Unicode-aware lower-casing meant for comparison ("ПОВІТРЯНА" == "повітряна").
Whitespace is NOT normalized: "повітряна  тривога" (two spaces) does not match "повітряна тривога".
Emoji are ordinary characters.

Pure functions, no database and no logging: the text of a rule or of a message is private and must never end up in a log line.
"""

import unicodedata
from collections.abc import Iterable


def fold(text: str) -> str:
    """The form in which rule phrases and message text are compared (NFC + casefold)."""
    return unicodedata.normalize("NFC", text).casefold()


def matches_any(keys: Iterable[str], text: str | None) -> bool:
    """
    True if the message `text` contains any already-folded rule phrase in `keys`.

    `keys` are ingest_ignore_rules.pattern_key values (folded when the rule was saved), `text` is raw message text.
    An empty or missing text never matches, and an empty key is ignored (a rule that matched everything would silently stop archiving a whole account).
    """
    if not text:
        return False
    folded = fold(text)
    return any(key and key in folded for key in keys)
