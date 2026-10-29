"""scripts/manage_admin.py - argument handling and prompts (the parts that need no database)."""

import sys
from typing import cast
from unittest import mock

from sqlalchemy.engine import Connection

import pytest

import scripts.manage_admin as ma

# _find_user() only forwards the connection to the (mocked) queries, so these tests need none; the cast keeps the type checker honest about that.
NO_CONNECTION = cast(Connection, None)


class TestFindUser:
    @pytest.fixture
    def cq(self):
        with mock.patch.object(ma.cq, "get_user_by_id") as by_id, mock.patch.object(ma.cq, "get_user_by_username") as by_name:
            by_id.return_value = {"id": 1, "username": "alice"}
            by_name.return_value = {"id": 2, "username": "bob"}
            yield by_id, by_name

    def test_id_takes_precedence_and_uses_the_id_lookup(self, cq):
        by_id, by_name = cq
        assert ma._find_user(NO_CONNECTION, 1, None)["username"] == "alice"
        by_id.assert_called_once()
        by_name.assert_not_called()

    def test_username_lookup(self, cq):
        by_id, by_name = cq
        assert ma._find_user(NO_CONNECTION, None, "bob")["id"] == 2
        by_id.assert_not_called()

    def test_unknown_user_exits_with_a_clear_message(self, cq):
        cq[0].return_value = None
        with pytest.raises(SystemExit) as exc:
            ma._find_user(NO_CONNECTION, 99, None)
        assert "No user found with id=99" in str(exc.value)

    def test_unknown_username_message_names_the_username(self, cq):
        cq[1].return_value = None
        with pytest.raises(SystemExit) as exc:
            ma._find_user(NO_CONNECTION, None, "ghost")
        assert "username='ghost'" in str(exc.value)

    def test_neither_id_nor_username_exits_instead_of_guessing(self, cq):
        with pytest.raises(SystemExit) as exc:
            ma._find_user(NO_CONNECTION, None, None)
        assert "--id or --username" in str(exc.value)
        cq[0].assert_not_called()
        cq[1].assert_not_called()

    def test_id_zero_is_a_real_id_not_falsy(self, cq):
        ma._find_user(NO_CONNECTION, 0, None)
        cq[0].assert_called_once()


class TestPasswordPrompt:
    def test_accepts_a_matching_password(self):
        with mock.patch("getpass.getpass", side_effect=["long-enough-pw", "long-enough-pw"]):
            assert ma._prompt_new_password() == "long-enough-pw"

    def test_rejects_a_short_password_before_asking_for_confirmation(self):
        with mock.patch("getpass.getpass", side_effect=["short"]) as gp:
            with pytest.raises(SystemExit):
                ma._prompt_new_password()
        assert gp.call_count == 1

    def test_rejects_a_mismatch(self):
        with mock.patch("getpass.getpass", side_effect=["long-enough-pw", "different-pw!!"]):
            with pytest.raises(SystemExit) as exc:
                ma._prompt_new_password()
        assert "did not match" in str(exc.value)

    def test_minimum_length_matches_the_registration_form(self):
        from api.schemas.auth import RegisterIn

        assert ma.MIN_PASSWORD_LENGTH == RegisterIn.model_fields["password"].metadata[0].min_length


class TestConfirm:
    @pytest.mark.parametrize("answer, expected", [("y", True), ("Y", True), (" y ", True), ("yes", False), ("n", False), ("", False)])
    def test_only_a_plain_y_confirms(self, answer, expected):
        with mock.patch("builtins.input", return_value=answer):
            assert ma._confirm("Proceed?") is expected


class TestCommandLine:
    def test_a_password_can_never_be_passed_on_the_command_line(self, monkeypatch, capsys):
        """It would end up in shell history and in `ps` output - the parser must simply not know the flag."""
        monkeypatch.setattr(sys, "argv", ["manage_admin.py", "create", "--username", "alice", "--password", "hunter2hunter2"])
        with pytest.raises(SystemExit) as exc:
            ma.main()
        assert exc.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    @pytest.mark.parametrize("removed", ["promote", "demote", "make-admin"])
    def test_there_is_no_way_to_create_a_second_admin(self, monkeypatch, removed):
        monkeypatch.setattr(sys, "argv", ["manage_admin.py", removed, "--username", "alice"])
        with pytest.raises(SystemExit) as exc:
            ma.main()
        assert exc.value.code == 2

    def test_there_is_no_non_interactive_confirmation_flag(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["manage_admin.py", "delete-admin", "--username", "alice", "--yes"])
        with pytest.raises(SystemExit) as exc:
            ma.main()
        assert exc.value.code == 2

    def test_target_flags_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["manage_admin.py", "set-archive", "--id", "1", "--username", "a", "--db-name", "x"])
        with pytest.raises(SystemExit) as exc:
            ma.main()
        assert exc.value.code == 2