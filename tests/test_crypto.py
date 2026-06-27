"""Fernet round-trip for OAuth-token-at-rest encryption (T15, T12)."""
from __future__ import annotations

from cryptography.fernet import Fernet

from app.services import crypto


def test_encrypt_decrypt_roundtrip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"fernet_key": key}))
    ciphertext = crypto.encrypt("ya29.super-secret-access-token")
    assert ciphertext != "ya29.super-secret-access-token"  # actually encrypted
    assert crypto.decrypt(ciphertext) == "ya29.super-secret-access-token"


def test_distinct_keys_cannot_decrypt(monkeypatch):
    k1 = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"fernet_key": k1}))
    ciphertext = crypto.encrypt("token")

    k2 = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: type("S", (), {"fernet_key": k2}))
    import pytest
    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        crypto.decrypt(ciphertext)
