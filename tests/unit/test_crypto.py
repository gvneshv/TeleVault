"""utils/crypto.py - Fernet encryption of the Telegram credential columns."""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from utils import crypto
from utils.crypto import decrypt_secret, encrypt_secret


def test_round_trip():
    assert decrypt_secret(encrypt_secret("my-api-hash")) == "my-api-hash"


def test_ciphertext_does_not_contain_the_plaintext():
    secret = "super-secret-session-string"
    token = encrypt_secret(secret)
    assert secret not in token and token != secret


def test_encryption_is_randomised_so_equal_secrets_do_not_look_equal():
    assert encrypt_secret("same") != encrypt_secret("same")


def test_none_passes_through_both_ways():
    assert encrypt_secret(None) is None
    assert decrypt_secret(None) is None


def test_empty_string_stays_distinguishable_from_none():
    token = encrypt_secret("")
    assert token is not None
    assert decrypt_secret(token) == ""


def test_unicode_survives():
    assert decrypt_secret(encrypt_secret("пароль-🔑")) == "пароль-🔑"


def test_wrong_key_raises_rather_than_returning_garbage(monkeypatch):
    token = encrypt_secret("value")
    monkeypatch.setattr(crypto, "_fernet", Fernet(Fernet.generate_key()))
    with pytest.raises(InvalidToken):
        decrypt_secret(token)


def test_tampered_ciphertext_raises():
    token = encrypt_secret("value")
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(InvalidToken):
        decrypt_secret(tampered)


def test_garbage_input_raises():
    with pytest.raises(InvalidToken):
        decrypt_secret("not-a-fernet-token")
