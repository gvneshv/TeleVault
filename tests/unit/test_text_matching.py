"""utils/text_matching.py - how an ignore rule's phrase is compared with a message's text."""

import unicodedata

import pytest

from utils.text_matching import fold, matches_any


class TestFold:
    def test_it_ignores_case_for_cyrillic(self):
        assert fold("ПОВІТРЯНА Тривога") == fold("повітряна тривога")

    def test_it_ignores_case_beyond_plain_lowercasing(self):
        """casefold(), not lower(): the German sharp s is the classic difference."""
        assert fold("STRASSE") == fold("straße")

    def test_the_same_letter_in_two_unicode_forms_compares_equal(self):
        composed = "Й"  # Cyrillic capital short I as one code point
        decomposed = unicodedata.normalize("NFD", composed)  # И + combining breve
        assert composed != decomposed
        assert fold(composed) == fold(decomposed)

    def test_it_does_not_collapse_whitespace(self):
        assert fold("a  b") != fold("a b")

    def test_ukrainian_and_russian_spellings_stay_different(self):
        """Two languages, two rules: the owner adds both if both are wanted."""
        assert fold("тривога") != fold("тревога")


class TestMatchesAny:
    KEYS = [fold("повітряна тривога")]

    def test_a_phrase_inside_a_longer_text_matches(self):
        assert matches_any(self.KEYS, "🚨 М. ХАРКІВ - ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА") is True

    def test_the_emoji_is_not_needed_in_the_rule(self):
        assert matches_any([fold("ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА")], "🚨 М. ХАРКІВ - ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА") is True

    def test_double_spaces_in_the_text_do_not_match(self):
        assert matches_any(self.KEYS, "повітряна  тривога") is False

    def test_a_different_word_ending_is_a_different_phrase(self):
        assert matches_any([fold("ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА")], "ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ") is False

    def test_any_one_rule_is_enough(self):
        assert matches_any([fold("ніколи"), fold("тривога")], "знову тривога") is True

    @pytest.mark.parametrize("text", [None, ""])
    def test_no_text_never_matches(self, text):
        assert matches_any(self.KEYS, text) is False

    def test_no_rules_never_match(self):
        assert matches_any([], "повітряна тривога") is False

    def test_an_empty_key_is_ignored_instead_of_matching_everything(self):
        assert matches_any([""], "anything at all") is False

    def test_it_is_a_substring_not_a_pattern(self):
        """Regex characters in a phrase are ordinary characters."""
        assert matches_any([fold("(a|b)")], "a") is False
        assert matches_any([fold("(a|b)")], "x (a|b) y") is True
        assert matches_any([fold(".*")], "anything") is False
