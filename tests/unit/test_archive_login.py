"""utils/archive_login.py: turning the stored (encrypted) archive login into a db.ArchiveLogin - and refusing, loudly, when it cannot."""

import pytest

import db
from utils.archive_login import ArchiveLoginError, archive_login_from_row, encrypt_archive_password


def test_no_login_stored_means_none():
    assert archive_login_from_row({"archive_db_user": None, "archive_db_password": None}) is None
    assert archive_login_from_row({}) is None  # a row without the columns at all (e.g. an older query)


def test_a_stored_login_round_trips():
    row = {"archive_db_user": "televault_archive_1", "archive_db_password": encrypt_archive_password("s3cret-pw")}
    assert archive_login_from_row(row) == db.ArchiveLogin(role="televault_archive_1", password="s3cret-pw")


def test_the_stored_value_is_not_the_password():
    assert "s3cret-pw" not in encrypt_archive_password("s3cret-pw")


@pytest.mark.parametrize(
    "row",
    [
        {"archive_db_user": "r", "archive_db_password": None},
        {"archive_db_user": None, "archive_db_password": "x"},
        {"archive_db_user": "", "archive_db_password": "x"},
    ],
)
def test_half_a_login_is_an_error_not_a_guess(row):
    with pytest.raises(ArchiveLoginError):
        archive_login_from_row(row)


def test_a_password_that_does_not_decrypt_is_an_error_that_leaks_nothing():
    with pytest.raises(ArchiveLoginError) as excinfo:
        archive_login_from_row({"archive_db_user": "r", "archive_db_password": "garbage-ciphertext"})
    assert "garbage-ciphertext" not in str(excinfo.value)


def test_the_password_is_hidden_from_repr_and_str():
    login = db.ArchiveLogin(role="r", password="hunter2-hunter2")
    assert "hunter2" not in repr(login) and "hunter2" not in str(login)
