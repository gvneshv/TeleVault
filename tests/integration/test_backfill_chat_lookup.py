"""
`backfill.py --chat <id>`: the chat is found in the account's own chat list first, and only otherwise asked for by id.

Why: the web UI's chat picker passes ids taken from the chat list.
A fresh backfill process has an empty entity cache, and Telegram resolves a bare group or channel id only with an access hash the chat list brings along;
the lookup by id alone is not dependable for those.
"""

from tests.integration.test_backfill_blocklist import stored, telegram  # noqa: F401  (shared fixture and helpers)


def test_a_chat_in_the_chat_list_is_found_there_without_asking_for_it_by_id(archive, telegram):  # noqa: F811
    fake, status = telegram({11: ["a"], 12: ["b"]}, chat_selector="12")
    assert fake.entity_lookups == []
    assert fake.fetched == [12] and stored(archive.conn) == {12: 1} and status["state"] == "completed"


def test_a_chat_that_is_not_in_the_chat_list_is_still_resolved_by_id(archive, telegram):  # noqa: F811
    fake, status = telegram({11: ["a"], 12: ["b"]}, chat_selector="12", setup=lambda f: f.not_in_dialogs.add(12))
    assert fake.entity_lookups == [12]
    assert fake.fetched == [12] and stored(archive.conn) == {12: 1} and status["state"] == "completed"


def test_a_channel_is_found_in_the_chat_list_too(archive, telegram):  # noqa: F811
    fake, _ = telegram({5: ["a"]}, channels=frozenset({5}), chat_selector="5")
    assert fake.entity_lookups == [] and fake.fetched == [5]
