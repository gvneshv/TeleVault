"""utils/security.py - password hashing and JWT issue/verify."""

from datetime import datetime, timedelta, timezone
from typing import Any, cast

import jwt
import pytest

from config import settings
from utils import security
from utils.security import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)


class TestPasswords:
    def test_correct_password_verifies(self):
        assert verify_password("hunter2hunter2", hash_password("hunter2hunter2"))

    def test_wrong_password_is_false_not_an_exception(self):
        assert verify_password("wrong-password", hash_password("hunter2hunter2")) is False

    def test_hash_is_not_the_password_and_is_salted(self):
        h1, h2 = hash_password("same-password"), hash_password("same-password")
        assert "same-password" not in h1
        assert h1 != h2

    def test_hash_uses_argon2(self):
        assert hash_password("x" * 12).startswith("$argon2")

    def test_long_and_unicode_passwords_work(self):
        for pw in ("p" * 1000, "пароль-довгий-🔐"):
            assert verify_password(pw, hash_password(pw))

    def test_passlib_refuses_passwords_over_4096_characters(self):
        """
        Pins a LIBRARY limit the API layer has to respect: passlib raises instead of hashing very long input.
        api/schemas/auth.py's docstring claims passlib "hashes the raw bytes regardless of length" - that is not true,
        so an unbounded password field turns an oversized password into an unhandled exception (see tests/api/test_auth_flow.py).
        """
        from passlib.exc import PasswordSizeError

        with pytest.raises(PasswordSizeError):
            hash_password("p" * 5000)
        with pytest.raises(PasswordSizeError):
            verify_password("p" * 5000, hash_password("fine-password"))


def _forge(payload: dict, secret: str | None = None) -> str:
    return jwt.encode(payload, secret or settings.jwt_secret, algorithm="HS256")


def _claims(kind: str, **extra) -> dict:
    now = datetime.now(timezone.utc)
    return {"type": kind, "sub": "5", "iat": now, "exp": now + timedelta(minutes=5), **extra}


class TestAccessTokens:
    def test_round_trip(self):
        assert decode_access_token(create_access_token(5, True)) == {"user_id": 5, "is_admin": True}
        assert decode_access_token(create_access_token(6, False)) == {"user_id": 6, "is_admin": False}

    def test_expired_token_is_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        token = _forge(_claims("access", is_admin=False, exp=past))
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_access_token(token)

    def test_token_signed_with_another_secret_is_rejected(self):
        with pytest.raises(jwt.InvalidSignatureError):
            decode_access_token(_forge(_claims("access", is_admin=True), secret="not-our-secret-" + "x" * 30))

    def test_refresh_token_cannot_be_used_as_an_access_token(self):
        refresh, _, _ = create_refresh_token(5)
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(refresh)

    def test_unsigned_alg_none_token_is_rejected(self):
        token = jwt.encode(_claims("access", is_admin=True), key=cast(Any, None), algorithm="none")  # the "none" algorithm takes no key; the stubs do not say so
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token(token)

    def test_garbage_is_rejected(self):
        with pytest.raises(jwt.InvalidTokenError):
            decode_access_token("definitely.not.a-jwt")

    def test_access_token_lifetime_is_short(self):
        payload = jwt.decode(create_access_token(1, False), settings.jwt_secret, algorithms=["HS256"])
        lifetime = payload["exp"] - payload["iat"]
        assert lifetime == security.ACCESS_TOKEN_MINUTES * 60 <= 15 * 60

    def test_tampering_with_the_admin_claim_breaks_the_signature(self):
        token = create_access_token(5, False)
        header, payload, signature = token.split(".")
        import base64, json

        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["is_admin"] = True
        forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        with pytest.raises(jwt.InvalidSignatureError):
            decode_access_token(f"{header}.{forged_payload}.{signature}")


class TestRefreshTokens:
    def test_round_trip_and_returned_metadata(self):
        token, jti, expires_at = create_refresh_token(9)
        decoded = decode_refresh_token(token)
        assert decoded == {"user_id": 9, "jti": jti}
        assert expires_at > datetime.now(timezone.utc) + timedelta(days=security.REFRESH_TOKEN_DAYS - 1)

    def test_each_token_has_a_unique_jti(self):
        assert create_refresh_token(1)[1] != create_refresh_token(1)[1]

    def test_access_token_cannot_be_used_as_a_refresh_token(self):
        with pytest.raises(jwt.InvalidTokenError):
            decode_refresh_token(create_access_token(5, False))

    def test_expired_refresh_token_is_rejected(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(jwt.ExpiredSignatureError):
            decode_refresh_token(_forge(_claims("refresh", jti="x", exp=past)))