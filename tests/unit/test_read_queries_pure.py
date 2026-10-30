"""db/read_queries.py - the helpers that never touch a database."""

import pytest

from db.read_queries import _is_whole_word_match, _like_pattern, _shape_message_row


class TestLikePattern:
    """User search text must be matched literally: LIKE wildcards in it must not widen the search."""

    def test_plain_text_is_wrapped_for_substring_match(self):
        assert _like_pattern("hello") == "%hello%"

    def test_percent_is_escaped(self):
        assert _like_pattern("100%") == "%100\\%%"

    def test_underscore_is_escaped(self):
        assert _like_pattern("a_b") == "%a\\_b%"

    def test_backslash_is_escaped_first_so_it_is_not_double_escaped(self):
        assert _like_pattern("a\\b") == "%a\\\\b%"
        assert _like_pattern("\\%") == "%\\\\\\%%"

    def test_sql_metacharacters_are_just_text(self):
        # Quotes are handled by parameter binding, not by this function; it must simply leave them alone.
        assert _like_pattern("'; DROP TABLE messages;--") == "%'; DROP TABLE messages;--%"

    def test_empty_string(self):
        assert _like_pattern("") == "%%"


class TestWholeWordMatch:
    def test_matches_standalone_word(self):
        assert _is_whole_word_match("the cat sat", "cat")

    def test_does_not_match_inside_a_longer_word(self):
        assert not _is_whole_word_match("concatenate", "cat")
        assert not _is_whole_word_match("category", "cat")

    def test_case_insensitive(self):
        assert _is_whole_word_match("The CAT sat", "cat")

    def test_punctuation_counts_as_a_boundary(self):
        assert _is_whole_word_match("cat, dog.", "cat")
        assert _is_whole_word_match("(cat)", "cat")

    def test_cyrillic_boundaries(self):
        assert _is_whole_word_match("мій кіт спить", "кіт")
        assert not _is_whole_word_match("котики", "кот")

    def test_regex_metacharacters_in_the_search_word_are_literal(self):
        assert _is_whole_word_match("price is a.b today", "a.b")
        assert not _is_whole_word_match("price is axb today", "a.b")
        assert not _is_whole_word_match("anything", "(")  # must not raise re.error

    def test_start_and_end_of_text(self):
        assert _is_whole_word_match("cat", "cat")


class TestShapeMessageRow:
    ROW = {
        "id": 7, "tg_message_id": 70, "chat_id": -100, "chat_name": "Team", "chat_type": "supergroup",
        "sender_id": 5, "sender_username": "bob", "sender_first_name": "Bob", "sender_last_name": None,
        "text": "hi", "date": "D", "archived_at": "A", "is_edited": False, "edited_at": None, "edit_count": 3, "is_deleted": True, "deleted_at": "X",
    }

    def test_nests_sender_and_chat(self):
        shaped = _shape_message_row(dict(self.ROW), has_chat_columns=True)
        assert shaped["sender"] == {"sender_id": 5, "username": "bob", "first_name": "Bob", "last_name": None, "display_name": None}
        assert shaped["chat"] == {"chat_id": -100, "name": "Team", "chat_type": "supergroup"}
        assert shaped["is_deleted"] is True and shaped["id"] == 7
        assert shaped["edit_count"] == 3

    def test_no_sender_gives_none(self):
        row = dict(self.ROW, sender_id=None)
        assert _shape_message_row(row, has_chat_columns=True)["sender"] is None

    def test_chat_is_omitted_when_the_query_did_not_join_it(self):
        assert _shape_message_row(dict(self.ROW), has_chat_columns=False)["chat"] is None

    def test_flat_keys_do_not_leak_into_the_shaped_row(self):
        shaped = _shape_message_row(dict(self.ROW), has_chat_columns=True)
        assert "sender_username" not in shaped and "chat_name" not in shaped