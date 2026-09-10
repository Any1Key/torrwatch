from __future__ import annotations

from torrwatch.core.security import hash_password, verify_password


def test_argon2_password_round_trip() -> None:
    password_hash = hash_password("a-strong-bootstrap-password")

    assert password_hash.startswith("$argon2")
    assert verify_password(password_hash, "a-strong-bootstrap-password")
    assert not verify_password(password_hash, "wrong-password")
